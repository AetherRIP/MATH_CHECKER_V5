"""Local-only browser smoke/layout audit; run against an isolated DEMO server."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:18742')
parser.add_argument('--output', default='docs/results/final_ui_checks.json')
args = parser.parse_args()
routes = ['dashboard','ai','tasks','correct','assignments','results','review','scheme-review',
          'automation','classroom','classes','students','reports','storage','usage',
          'privacy','settings','distributed']
sizes = [(800,600),(1024,768),(1280,720),(1366,768),(1600,900),(1920,1080)]
records, errors = [], []
with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(args.url)
    if page.get_by_role('button', name="Skip setup — I'll configure later").count():
        page.get_by_role('button', name="Skip setup — I'll configure later").click()
    for width,height in sizes:
        page.set_viewport_size({'width':width,'height':height})
        for route in routes:
            page.goto(args.url+'/#/'+route)
            page.wait_for_timeout(350)
            data = page.evaluate("""() => ({width:innerWidth,height:innerHeight,
              overflow:document.documentElement.scrollWidth>innerWidth+2,
              headings:[...document.querySelectorAll('h1,h2,h3')].map(e=>e.textContent),
              buttons:document.querySelectorAll('button').length,
              scroll:document.documentElement.scrollWidth})""")
            records.append({'route':route,**data})
    page.goto(args.url+'/#/review')
    page.wait_for_timeout(700)
    badge = page.get_by_role('button', name='Review Queue', exact=False).first.inner_text()
    rows = page.locator('tbody tr').count()
    review_matches = (str(rows) in badge) if rows else not any(c.isdigit() for c in badge)
    page.goto(args.url+'/#/automation')
    page.get_by_role('button', name='Check now', exact=True).click()
    page.wait_for_timeout(700)
    clear = page.get_by_role('button', name='Clear activity', exact=True)
    clear.click()
    page.get_by_role('button', name='Clear visible activity', exact=True).click()
    page.get_by_text('Visible activity cleared; jobs and audit history preserved.', exact=True).wait_for()
    clear_verified = True
    page.goto(args.url+'/#/distributed')
    page.get_by_text('Multi-School — Work in Progress', exact=True).wait_for()
    browser.close()
result={'checks':records,'page_errors':errors,'wip_cover':True, 'review_count_matches': review_matches, 'clear_dialog_workflow': clear_verified,
        'failures':[r for r in records if r['overflow'] or not r['headings']]}
Path(args.output).parent.mkdir(parents=True,exist_ok=True)
Path(args.output).write_text(json.dumps(result,indent=2),encoding='utf-8')
print(f"{len(records)} page/viewport checks; {len(result['failures'])} layout failures; {len(errors)} page errors")
raise SystemExit(bool(result['failures'] or errors or not review_matches))
