### Overview
Python script to enumerate WordPress users across multiple attack vectors simultaneously: XML-RPC, REST API, and author page redirects. 

### Features

- **XML-RPC enumeration** via `wp.getAuthors`, `wp.getUsers`, and `system.multicall`
- **REST API fallback** via `/wp-json/wp/v2/users`
- **Author page enumeration** via `/?author=N` redirect analysis
- **Username inference** via multicall fault code analysis 
- **Multi-target scanning** with configurable threading
- **JSON report export** and plain-text username list output
- **Automatic deduplication** across all discovery methods


### Usage

### Single Target
```bash
python3 wp_xmlrpc_enum.py -u https://target.com
```

### Multiple Targets from File
```bash
python3 wp_xmlrpc_enum.py -f urls.txt
```

### With All Options
```bash
python3 wp_xmlrpc_enum.py -u https://target.com \
  -t 10 \           # threads
  -T 20 \           # timeout (seconds)
  -v \              # verbose output
  --delay 1.5 \     # delay between requests
  -o report.json \  # save JSON report
  -w users.txt      # save username list
```

---
