"""Shared prompt fragments."""

NEGATIVE_CLAIM_RULE = (
    "When you claim something does NOT exist (no caller, no usage, not "
    "implemented), say which searches you ran to reach that conclusion - a "
    "negative claim is only as trustworthy as the search behind it."
)
TUI_FORMATTING = (
    "Keep the final answer compact and terminal-friendly: short paragraphs or "
    "bullets, no walls of text, no restating the question."
)

# Dangerous-API checklist distilled from Anthropic claude-plugins-official
# security-guidance/hooks/patterns.py (Apache-2.0, attributed in README).
SECURITY_PATTERN_CHECKLIST = """\
- eval() / new Function() / document.write() / innerHTML / outerHTML /
  insertAdjacentHTML / dangerouslySetInnerHTML fed with untrusted input (XSS /
  code injection).
- child_process.exec / execSync / os.system / subprocess(shell=True) / go exec
  through a shell - prefer argument arrays / shell=False; flag interpolated
  untrusted input.
- deserialization: pickle / marshal.loads / shelve / torch.load
  (weights_only=False) / unsafe yaml.load of untrusted data.
- crypto: homemade crypto, MD5/SHA-1 for passwords, AES-ECB, missing IV/nonce,
  TLS certificate verification disabled.
- XML: parsing untrusted XML without hardening (XXE) via xml.etree / lxml.
- GitHub Actions / CI workflow: interpolating issue/PR/event fields into run:
  or ref: (command / ref injection).
- script/link tags without SRI / subresource integrity.
Flag each construct only where it can be reached by untrusted input or weakens
a security control."""
