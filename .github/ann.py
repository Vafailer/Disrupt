import re
d = open('out.txt').read()
def esc(s): return s.replace('%', '%25').replace('\r', '').replace('\n', '%0A')
summary = [l for l in d.splitlines() if l.startswith(('FAILED', 'ERROR')) or re.search(r'\d+ (passed|failed)', l)]
print('::error title=pytest-summary::' + esc('\n'.join(summary[-60:]) or d[-3000:]))
i = d.find('= FAILURES =')
parts = re.split(r'\n_{3,} (.+?) _{3,}\n', d[i:]) if i >= 0 else []
for n in range(1, min(len(parts), 17), 2):
    body = parts[n + 1]
    lines = [l for l in body.splitlines() if l.startswith(('E ', '>')) or re.match(r'^\S+\.py:\d+', l)]
    print('::warning title=' + parts[n][:80].replace(':', ' ').replace(',', ' ') + '::' + esc('\n'.join(lines[-40:])[-3500:]))
