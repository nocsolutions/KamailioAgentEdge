#!/usr/bin/env python3
"""Print full SIP headers for messages matching a Call-ID substring + first-line filter.

  sipdump.py <file.pcap> <call-id-substring> [first-line-substring]
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sipflow import packets  # noqa: E402


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    path, want = sys.argv[1], sys.argv[2]
    firstwant = sys.argv[3] if len(sys.argv) > 3 else None
    shown = set()
    for ts, src, dst, payload in packets(path):
        text = payload.decode('utf-8', 'replace')
        if want not in text:
            continue
        lines = text.split('\r\n')
        first = lines[0]
        if firstwant and firstwant not in first:
            continue
        key = (first, src, dst)
        if key in shown:
            continue
        shown.add(key)
        print(f'\n### {src} -> {dst}\n{first}')
        for ln in lines[1:]:
            if not ln:
                break
            print(f'    {ln}')


if __name__ == '__main__':
    main()
