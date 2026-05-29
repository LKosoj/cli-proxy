import re
with open('miniapp/routes.py', 'r') as f:
    content = f.read()

match = re.search(r'def _extract_active_session_payload.*?return \{.*?"resume_tokens"', content, re.DOTALL | re.MULTILINE)
if match:
    print(match.group(0))
else:
    print("Could not find the payload dict")
