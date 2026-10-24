"""Pull the options the rules care about out of PodmanArgs= lines.

PodmanArgs is free-form, so this only recognises a fixed set of `podman run`
/ `podman pod create` options. Each is returned as (canonical name, value,
entry); boolean flags get the value "true" unless written --flag=false.
"""

import shlex

# Options that take a value. Aliases map to the canonical long name.
VALUE_OPTIONS = {
    "--network": "--network", "--net": "--network",
    "--pid": "--pid", "--ipc": "--ipc", "--uts": "--uts", "--cgroupns": "--cgroupns",
    "--userns": "--userns",
    "--security-opt": "--security-opt",
    "--cap-add": "--cap-add",
    "--volume": "--volume", "-v": "--volume",
    "--mount": "--mount",
    "--stop-timeout": "--stop-timeout",
    "--health-cmd": "--health-cmd",
    "--restart": "--restart",
    "--device": "--device",
}
FLAG_OPTIONS = {"--privileged", "--read-only", "--no-healthcheck"}


def split(value):
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def options(entries):
    out = []
    for entry in entries:
        tokens = split(entry.value)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            i += 1
            name, eq, val = tok.partition("=")
            if name in FLAG_OPTIONS:
                out.append((name, val.lower() if eq else "true", entry))
            elif name in VALUE_OPTIONS:
                canon = VALUE_OPTIONS[name]
                if eq:
                    out.append((canon, val, entry))
                elif i < len(tokens):
                    out.append((canon, tokens[i], entry))
                    i += 1
            elif tok.startswith("-v") and len(tok) > 2 and not tok.startswith("--"):
                out.append(("--volume", tok[2:], entry))
    return out
