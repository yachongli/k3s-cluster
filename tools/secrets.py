#!/usr/bin/env python3
"""
k3s-cluster secrets management tool (kolla-style).

Plaintext file is the source of truth. Ansible auto-loads it at runtime.

  /etc/k3s-cluster/passwords.yaml          ← plaintext (user edits this)
  inventory/cluster/group_vars/all/passwords.yaml  ← vault-encrypted (fallback for Git)

Ansible playbooks automatically check for /etc/k3s-cluster/passwords.yaml
at startup. If it exists, its values override the vault-encrypted version.
No manual sync needed.

Commands:
  init    — Create /etc/k3s-cluster/passwords.yaml with UUID internal passwords
            + empty values for external credentials (fill if needed).
  edit    — Open the plaintext file in $EDITOR.
  decrypt — Decrypt vault → plaintext (recover if plaintext is lost).
  list    — Show status of all credentials (plaintext file).

Usage:
  python3 tools/secrets.py init              # create plaintext file with UUID + empty externals
  python3 tools/secrets.py init --force      # regenerate all internal passwords
  python3 tools/secrets.py edit              # open plaintext in $EDITOR
  python3 tools/secrets.py decrypt           # vault → plaintext (recovery)
  python3 tools/secrets.py list              # show plaintext status
"""

import os
import re
import sys
import uuid
import subprocess
from pathlib import Path
from getpass import getpass

ROOT = Path(__file__).resolve().parent.parent
VAULT_YAML = ROOT / "inventory" / "cluster" / "group_vars" / "all" / "passwords.yaml"
GLOBAL_YAML = ROOT / "inventory" / "cluster" / "group_vars" / "all" / "globals.yaml"
PLAINTEXT_YAML = Path("/etc/k3s-cluster/passwords.yaml")

PLACEHOLDER = "CHANGE_ME"

# ── Credential registry ──────────────────────────────────────────────

CREDENTIALS = [
    {"var": "password_argocd_admin",   "type": "internal", "component": "ArgoCD admin",       "enable_key": "argo-cd"},
    {"var": "password_argocd_user",    "type": "internal", "component": "ArgoCD user",          "enable_key": "argo-cd"},
    {"var": "password_grafana_admin",  "type": "internal", "component": "Grafana admin",       "enable_key": "victoria-metrics"},
    {"var": "credential_cloudflare_api_token",     "type": "external", "component": "ExternalDNS (Cloudflare)","enable_key": "external-dns", "subfeature": None,             "required": False, "label": "Cloudflare API token",         "help": "Create at https://dash.cloudflare.com/profile/api-tokens"},
    {"var": "credential_longhorn_backup_password", "type": "external", "component": "Longhorn Backup (NAS)",  "enable_key": None,          "subfeature": "longhorn-backup", "required": False, "label": "NAS/CIFS backup password",     "help": "Leave empty to disable backup"},
    {"var": "credential_slack_webhook_url",         "type": "external", "component": "Kured Slack",           "enable_key": None,          "subfeature": "kured-slack",    "required": False, "label": "Slack webhook URL",            "help": "https://hooks.slack.com/services/... Leave empty to disable"},
    {"var": "credential_postfix_alias",            "type": "external", "component": "Postfix Email (iCloud)", "enable_key": None,          "subfeature": "postfix",        "required": False, "label": "iCloud email alias",            "help": "alias@icloud.com format"},
    {"var": "credential_postfix_name",             "type": "external", "component": "Postfix Email (iCloud)", "enable_key": None,          "subfeature": "postfix",        "required": False, "label": "iCloud email address",          "help": "username@icloud.com format"},
    {"var": "credential_postfix_password",         "type": "external", "component": "Postfix Email (iCloud)", "enable_key": None,          "subfeature": "postfix",        "required": False, "label": "iCloud app-specific password", "help": "Create at https://appleid.apple.com"},
    {"var": "credential_ceph_admin_key",           "type": "external", "component": "Ceph CSI",               "enable_key": "ceph-csi",    "subfeature": None,             "required": False, "label": "Ceph admin key (cephx)",        "help": "Generate with: ceph auth get-key client.admin"},
]

SUBFEATURE_DISABLE = {
    "longhorn-backup": {"file": "roles/longhorn/defaults/main.yaml", "pattern": r"(backup:\s*\n\s+enabled:\s*)true",  "replace": r"\1false", "label": "Longhorn backup"},
    "kured-slack":     {"file": "roles/kured/defaults/main.yaml",     "pattern": r"(slack:\s*\n\s+enabled:\s*)true",   "replace": r"\1false", "label": "Kured Slack"},
    "postfix":         {"file": "roles/cluster/defaults/main.yaml",  "pattern": r"(postfix:\s*\n\s+enabled:\s*)true", "replace": r"\1false", "label": "Postfix email"},
}


# ── Shared helpers ───────────────────────────────────────────────────

def check_prerequisites():
    if sys.version_info < (3, 6):
        sys.exit("Error: Python 3.6+ required.")
    result = subprocess.run(["ansible-vault", "--version"], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("Error: ansible-vault not found.\nInstall with: pip install ansible")
    if not VAULT_YAML.exists():
        sys.exit(f"Error: {VAULT_YAML} not found.")


def get_vault_pass_file():
    env_file = os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE")
    if env_file and Path(env_file).exists():
        return Path(env_file)
    default = Path.home() / ".vault_pass.txt"
    if default.exists():
        return default
    print("\n" + "=" * 60)
    print("Vault password file not found.")
    print(f"  Checked: $ANSIBLE_VAULT_PASSWORD_FILE = {env_file or '(not set)'}")
    print(f"  Checked: {default}")
    print()
    pw = getpass("Create vault password (encrypts all secrets): ")
    pw2 = getpass("Confirm: ")
    if pw != pw2:
        sys.exit("Error: passwords do not match.")
    if not pw:
        sys.exit("Error: password cannot be empty.")
    default.write_text(pw + "\n")
    try:
        default.chmod(0o600)
    except OSError:
        pass
    print(f"\nCreated: {default}")
    print(f'To persist: export ANSIBLE_VAULT_PASSWORD_FILE="{default}"')
    return default


def encrypt_string(value, name, vault_pass_file):
    result = subprocess.run(
        ["ansible-vault", "encrypt_string", value, "--name", name,
         "--vault-pass-file", str(vault_pass_file)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"Error encrypting {name}:\n{result.stderr}")
    return result.stdout.rstrip().replace("\r\n", "\n")


def decrypt_vault_block(vault_text, vault_pass_file):
    result = subprocess.run(
        ["ansible-vault", "decrypt", "--vault-pass-file", str(vault_pass_file)],
        input=vault_text,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def find_vault_blocks(text):
    lines = text.split("\n")
    blocks = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r'^(\s*)(\S+):\s*(!vault\s*\|)\s*$', line)
        if not m:
            continue
        indent = len(m.group(1))
        var_name = m.group(2)
        end = i + 1
        while end < len(lines):
            if not lines[end].strip():
                end += 1
                continue
            next_indent = len(lines[end]) - len(lines[end].lstrip())
            if next_indent <= indent:
                break
            end += 1
        blocks[var_name] = (i, end, indent)
    return blocks, lines


def reindent_block(ansible_output, target_indent):
    out_lines = ansible_output.split("\n")
    result = []
    for i, line in enumerate(out_lines):
        stripped = line.lstrip()
        if not stripped:
            result.append("")
            continue
        if i == 0:
            result.append(" " * target_indent + stripped)
        else:
            result.append(" " * (target_indent + 2) + stripped)
    return "\n".join(result)


def parse_plaintext_yaml(text):
    """Parse plaintext passwords.yaml, return {var_name: value}."""
    values = {}
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r'^(\S+):\s*(.*)$', line)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


def update_global_enable(enable_key, enabled):
    if not GLOBAL_YAML.exists():
        return False
    text = GLOBAL_YAML.read_text()
    flat_key = "enable_" + enable_key.replace("-", "_")
    pattern = rf'^({re.escape(flat_key)}):\s*\S+'
    replacement = f'\\1: {str(enabled).lower()}'
    new_text = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if new_text != text:
        GLOBAL_YAML.write_text(new_text)
        return True
    return False


def disable_subfeature(subfeature_key):
    info = SUBFEATURE_DISABLE.get(subfeature_key)
    if not info:
        return False
    file_path = ROOT / info["file"]
    if not file_path.exists():
        return False
    text = file_path.read_text()
    new_text = re.sub(info["pattern"], info["replace"], text, count=1)
    if new_text != text:
        file_path.write_text(new_text)
        print(f"  Disabled {info['label']} in {info['file']}")
        return True
    return False


def generate_plaintext_file(force=False):
    """Generate /etc/k3s-cluster/passwords.yaml (plaintext source of truth)."""
    PLAINTEXT_YAML.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if PLAINTEXT_YAML.exists() and not force:
        existing = parse_plaintext_yaml(PLAINTEXT_YAML.read_text())

    lines = [
        "# k3s-cluster plaintext passwords (source of truth)",
        "# Edit this file, then run: python3 tools/secrets.py sync",
        "# WARNING: This file contains plaintext secrets. Do NOT commit to Git.",
        "# File permissions should be 600 (set automatically).",
        "",
    ]

    generated = []
    current_component = None

    for cred in CREDENTIALS:
        comp = cred["component"]
        if comp != current_component:
            current_component = comp
            lines.append(f"# --- {comp} ({cred['type']}) ---")

        var = cred["var"]
        if var in existing and existing[var] not in ("", PLACEHOLDER) and not force:
            value = existing[var]
            lines.append(f"{var}: {value}")
        elif cred["type"] == "internal":
            pw = uuid.uuid4().hex
            lines.append(f"{var}: {pw}")
            generated.append((var, pw))
        else:
            lines.append(f"{var}:")

    lines.append("")

    PLAINTEXT_YAML.write_text("\n".join(lines))
    try:
        PLAINTEXT_YAML.chmod(0o600)
    except OSError:
        pass

    return generated


# ── init ────────────────────────────────────────────────────────────

def cmd_init(vault_pass, force=False):
    print("\n" + "=" * 60)
    print("k3s-cluster Secrets Init")
    print("=" * 60)
    print(f"  Plaintext file: {PLAINTEXT_YAML}")
    print(f"  Vault file:     {VAULT_YAML}")
    print(f"  Mode:            {'force (regenerate all)' if force else 'generate missing only'}")
    print()

    generated = generate_plaintext_file(force=force)

    for var, pw in generated:
        print(f"  GEN   {var} = {pw[:8]}...{pw[-4:]} (UUID4)")

    externals = sum(1 for c in CREDENTIALS if c["type"] == "external")
    print(f"\n  Plaintext file created: {PLAINTEXT_YAML}")
    print(f"  Internal passwords generated: {len(generated)}")
    print(f"  External credentials (empty, fill if needed): {externals}")

    print()
    print("Next steps:")
    print(f"  1. Edit plaintext:   {PLAINTEXT_YAML}")
    print(f"     (or run: python3 tools/secrets.py edit)")
    print(f"  2. Deploy:           ansible-playbook provisioning.yaml")
    print(f"     (Ansible auto-loads {PLAINTEXT_YAML} — no sync needed)")


# ── edit ────────────────────────────────────────────────────────────

def cmd_edit():
    if not PLAINTEXT_YAML.exists():
        sys.exit(f"Error: {PLAINTEXT_YAML} not found. Run 'init' first.")

    editor = os.environ.get("EDITOR", "vi")
    print(f"Opening {PLAINTEXT_YAML} with {editor}...")
    os.execvp(editor, [editor, str(PLAINTEXT_YAML)])


# ── sync (plaintext → vault) ────────────────────────────────────────

def cmd_sync(vault_pass):
    if not PLAINTEXT_YAML.exists():
        sys.exit(f"Error: {PLAINTEXT_YAML} not found. Run 'init' first.")

    plaintext = parse_plaintext_yaml(PLAINTEXT_YAML.read_text())
    vault_text = VAULT_YAML.read_text()
    blocks, lines = find_vault_blocks(vault_text)

    print("\n" + "=" * 60)
    print("k3s-cluster Secrets Sync (plaintext → vault)")
    print("=" * 60)
    print(f"  Plaintext: {PLAINTEXT_YAML}")
    print(f"  Vault:     {VAULT_YAML}")
    print()

    replacements = []
    changes = []

    for cred in CREDENTIALS:
        var = cred["var"]
        if var not in plaintext:
            print(f"  SKIP  {var} (not in plaintext)")
            continue
        if var not in blocks:
            print(f"  SKIP  {var} (not in vault template)")
            continue

        value = plaintext[var]
        if not value or value == PLACEHOLDER:
            print(f"  SKIP  {var} (empty)")
            # Disable component if external and left empty
            if cred["type"] == "external":
                if cred.get("enable_key"):
                    if update_global_enable(cred["enable_key"], False):
                        changes.append(f"Disabled '{cred['enable_key']}' (empty)")
                        print(f"        -> Disabled '{cred['enable_key']}' in globals.yaml")
                if cred.get("subfeature"):
                    if disable_subfeature(cred["subfeature"]):
                        changes.append(f"Disabled {cred['subfeature']} (empty)")
            continue

        encrypted = encrypt_string(value, var, vault_pass)
        start, end, indent = blocks[var]
        reindented = reindent_block(encrypted, indent)
        replacements.append((start, end, reindented))
        changes.append(f"Encrypted: {var}")
        masked = value[:8] + "..." + value[-4:] if len(value) > 16 else value
        print(f"  SYNC  {var} = {masked}")

    if replacements:
        backup = VAULT_YAML.with_suffix(".yaml.bak")
        backup.write_text(vault_text)
        replacements.sort(key=lambda x: x[0], reverse=True)
        for start, end, new_text in replacements:
            new_lines = new_text.split("\n")
            lines = lines[:start] + new_lines + lines[end:]
        VAULT_YAML.write_text("\n".join(lines))
        print(f"\n  Backup saved: {backup}")

    print(f"\n  Synced: {len(replacements)} values")
    if any("Disabled" in c for c in changes):
        print(f"  Disabled: {sum(1 for c in changes if 'Disabled' in c)} components (still CHANGE_ME)")

    print()
    print("Next steps:")
    print("  1. Review:     git diff")
    print("  2. Validate:   ansible-playbook validation.yaml")
    print("  3. Deploy:     ansible-playbook provisioning.yaml")


# ── decrypt (vault → plaintext) ─────────────────────────────────────

def cmd_decrypt(vault_pass):
    vault_text = VAULT_YAML.read_text()
    blocks, lines = find_vault_blocks(vault_text)

    print("\n" + "=" * 60)
    print("k3s-cluster Secrets Decrypt (vault → plaintext)")
    print("=" * 60)
    print(f"  Vault:     {VAULT_YAML}")
    print(f"  Plaintext: {PLAINTEXT_YAML}")
    print()

    PLAINTEXT_YAML.parent.mkdir(parents=True, exist_ok=True)

    out_lines = [
        "# k3s-cluster plaintext passwords (recovered from vault)",
        "# Edit this file, then run: python3 tools/secrets.py sync",
        "# WARNING: This file contains plaintext secrets. Do NOT commit to Git.",
        "",
    ]

    current_component = None
    for cred in CREDENTIALS:
        var = cred["var"]
        comp = cred["component"]
        if comp != current_component:
            current_component = comp
            out_lines.append(f"# --- {comp} ({cred['type']}) ---")

        if var not in blocks:
            out_lines.append(f"# {var}: MISSING (not in vault)")
            print(f"  SKIP  {var} (not in vault)")
            continue

        start, end, indent = blocks[var]
        vault_text_block = "\n".join(lines[start:end])
        plaintext = decrypt_vault_block(vault_text_block, vault_pass)

        if plaintext is None:
            out_lines.append(f"# {var}: DECRYPT_ERROR")
            print(f"  ERROR {var} (decrypt failed)")
            continue

        out_lines.append(f"{var}: {plaintext}")
        masked = plaintext[:8] + "..." + plaintext[-4:] if len(plaintext) > 16 else plaintext
        print(f"  DECRYPT  {var} = {masked}")

    out_lines.append("")
    PLAINTEXT_YAML.write_text("\n".join(out_lines))
    try:
        PLAINTEXT_YAML.chmod(0o600)
    except OSError:
        pass

    print(f"\n  Plaintext file written: {PLAINTEXT_YAML}")
    print()
    print("Next steps:")
    print("  1. Edit if needed: python3 tools/secrets.py edit")
    print("  2. Re-sync:        python3 tools/secrets.py sync")


# ── list ────────────────────────────────────────────────────────────

def cmd_list():
    if not PLAINTEXT_YAML.exists():
        print(f"\n  Plaintext file not found: {PLAINTEXT_YAML}")
        print("  Run 'python3 tools/secrets.py init' first.")
        return

    plaintext = parse_plaintext_yaml(PLAINTEXT_YAML.read_text())

    print("\n" + "=" * 60)
    print("k3s-cluster Secrets Status (plaintext)")
    print("=" * 60)
    print(f"  File: {PLAINTEXT_YAML}")
    print()

    ready = 0
    pending = 0

    for cred in CREDENTIALS:
        var = cred["var"]
        ctype = cred["type"].upper()
        comp = cred["component"]
        if var not in plaintext:
            print(f"  [MISSING ] [{ctype:8s}] {var:45s} ({comp})")
            pending += 1
        elif not plaintext[var] or plaintext[var] == PLACEHOLDER:
            if cred.get("required", False):
                print(f"  [PENDING ] [{ctype:8s}] {var:45s} ({comp})")
                pending += 1
            else:
                print(f"  [EMPTY   ] [{ctype:8s}] {var:45s} ({comp})")
        else:
            val = plaintext[var]
            masked = val[:8] + "..." + val[-4:] if len(val) > 16 else val
            print(f"  [READY   ] [{ctype:8s}] {var:45s} = {masked} ({comp})")
            ready += 1

    print()
    print(f"  Ready: {ready}  Pending: {pending}")
    if pending > 0:
        print()
        print("  Pending values need to be filled in:")
        print(f"    1. Edit: python3 tools/secrets.py edit")
        print(f"    2. Sync: python3 tools/secrets.py sync")


# ── diff ────────────────────────────────────────────────────────────

def cmd_diff(vault_pass):
    if not PLAINTEXT_YAML.exists():
        print(f"\n  Plaintext file not found: {PLAINTEXT_YAML}")
        return

    plaintext = parse_plaintext_yaml(PLAINTEXT_YAML.read_text())
    vault_text = VAULT_YAML.read_text()
    blocks, lines = find_vault_blocks(vault_text)

    print("\n" + "=" * 60)
    print("k3s-cluster Secrets Diff (plaintext vs vault)")
    print("=" * 60)
    print()

    differences = 0
    for cred in CREDENTIALS:
        var = cred["var"]
        if var not in plaintext:
            continue
        if var not in blocks:
            continue

        pt_val = plaintext[var]
        if not pt_val or pt_val == PLACEHOLDER:
            continue

        start, end, indent = blocks[var]
        vault_block = "\n".join(lines[start:end])
        vault_val = decrypt_vault_block(vault_block, vault_pass)

        if vault_val is None:
            print(f"  DIFF  {var}: plaintext=set, vault=ERROR")
            differences += 1
        elif pt_val != vault_val:
            pt_masked = pt_val[:8] + "..." + pt_val[-4:] if len(pt_val) > 16 else pt_val
            vt_masked = vault_val[:8] + "..." + vault_val[-4:] if len(vault_val) > 16 else vault_val
            print(f"  DIFF  {var}: plaintext={pt_masked}  vault={vt_masked}")
            differences += 1

    if differences == 0:
        print("  All values match. No sync needed.")
    else:
        print(f"\n  {differences} value(s) differ. Run: python3 tools/secrets.py sync")


# ── main ────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("--help", "-h"):
        print(__doc__)
        return

    cmd = args[0]

    # Commands that don't need vault password
    if cmd == "edit":
        cmd_edit()
        return
    if cmd == "list":
        cmd_list()
        return

    # Commands that need vault password
    check_prerequisites()
    vault_pass = get_vault_pass_file()

    if cmd == "init":
        cmd_init(vault_pass, force="--force" in args)
    elif cmd == "decrypt":
        cmd_decrypt(vault_pass)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(1)
