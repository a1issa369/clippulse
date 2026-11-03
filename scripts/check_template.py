"""Offline sanity check for template.yaml (CI also runs cfn-lint when it is installed).

Catches the mistakes that only show up mid-deploy: a !Ref to something that does
not exist, a !GetAtt on a missing resource, a ${Name} in a !Sub nothing defines,
a DependsOn typo, or an Output pointing nowhere.
"""
import re
import sys
from pathlib import Path

import yaml

PSEUDO = {"AWS::Region", "AWS::AccountId", "AWS::StackName", "AWS::StackId", "AWS::Partition", "AWS::NoValue", "AWS::URLSuffix"}


class Tagged:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value


def loader():
    class L(yaml.SafeLoader):
        pass

    def multi(l, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            value = l.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = l.construct_sequence(node, deep=True)
        else:
            value = l.construct_mapping(node, deep=True)
        return Tagged(suffix, value)

    L.add_multi_constructor("!", multi)
    return L


def walk(node):
    if isinstance(node, Tagged):
        yield node
        yield from walk(node.value)
    elif isinstance(node, dict):
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def main(path: str) -> int:
    doc = yaml.load(Path(path).read_text(), Loader=loader())
    params, resources = set(doc.get("Parameters", {})), set(doc["Resources"])
    conditions = set(doc.get("Conditions", {}))
    errors = []

    def known(name):
        return name in params or name in resources or name in PSEUDO

    for tag in walk(doc):
        if tag.tag == "Ref":
            if not known(tag.value):
                errors.append(f"!Ref {tag.value}: no such parameter or resource")
        elif tag.tag == "GetAtt":
            target = tag.value.split(".")[0] if isinstance(tag.value, str) else tag.value[0]
            if target not in resources:
                errors.append(f"!GetAtt {tag.value}: no such resource")
        elif tag.tag == "Sub":
            text = tag.value if isinstance(tag.value, str) else tag.value[0]
            local = set(tag.value[1]) if isinstance(tag.value, list) else set()
            for var in re.findall(r"\$\{([^}]+)\}", text):
                root = var.split(".")[0]
                if root not in local and not known(root):
                    errors.append(f"!Sub ${{{var}}}: undefined")
        elif tag.tag == "Condition" and tag.value not in conditions:
            errors.append(f"!Condition {tag.value}: undefined")
        elif tag.tag == "If":
            if not isinstance(tag.value, list) or len(tag.value) != 3:
                errors.append("!If needs [condition, then, else]")
            elif tag.value[0] not in conditions:
                errors.append(f"!If {tag.value[0]}: undefined condition")

    for name, res in doc["Resources"].items():
        for dep in ([res.get("DependsOn")] if isinstance(res.get("DependsOn"), str) else res.get("DependsOn", [])):
            if dep not in resources:
                errors.append(f"{name}.DependsOn {dep}: no such resource")
        cond = res.get("Condition")
        if cond and cond not in conditions:
            errors.append(f"{name}.Condition {cond}: undefined")
        if "Type" not in res:
            errors.append(f"{name}: missing Type")

    # A resource that only exists under a condition must not be used by anything unconditional.
    conditional = {n for n, r in doc["Resources"].items() if r.get("Condition")}

    def uses(node, skip_if=False):
        """Names of resources referenced, ignoring anything inside an !If (it guards its own branches)."""
        found = set()
        if isinstance(node, Tagged):
            if node.tag == "If" and skip_if:
                return found
            if node.tag == "Ref" and isinstance(node.value, str):
                found.add(node.value)
            elif node.tag == "GetAtt":
                found.add(node.value.split(".")[0] if isinstance(node.value, str) else node.value[0])
            elif node.tag == "Sub":
                text = node.value if isinstance(node.value, str) else node.value[0]
                found |= {v.split(".")[0] for v in re.findall(r"\$\{([^}]+)\}", text)}
            found |= uses(node.value, skip_if)
        elif isinstance(node, dict):
            for v in node.values():
                found |= uses(v, skip_if)
        elif isinstance(node, list):
            for v in node:
                found |= uses(v, skip_if)
        return found

    for name, res in doc["Resources"].items():
        if not res.get("Condition"):
            for bad in sorted(uses(res, skip_if=True) & conditional):
                errors.append(f"{name} (unconditional) uses conditional resource {bad}")
    for name, out in doc.get("Outputs", {}).items():
        if not out.get("Value"):
            errors.append(f"Output {name}: missing Value")
        if out.get("Condition") and out["Condition"] not in conditions:
            errors.append(f"Output {name}.Condition {out['Condition']}: undefined")
        if not out.get("Condition"):
            for bad in sorted(uses(out.get("Value"), skip_if=True) & conditional):
                errors.append(f"Output {name} (unconditional) uses conditional resource {bad}")

    for message in errors:
        print("ERROR", message)
    print(f"{len(resources)} resources, {len(params)} parameters checked: {'FAILED' if errors else 'ok'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "template.yaml"))
