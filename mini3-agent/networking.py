"""Choose an active physical IPv4 interface, never a VPN/WSL default route."""
import ipaddress
import json
import subprocess
import sys


def choose_address(rows):
    addresses = set()
    for row in rows:
        try:
            ip = ipaddress.IPv4Address(row['IPAddress'])
        except (KeyError, ValueError, TypeError):
            continue
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast):
            addresses.add(str(ip))
    if len(addresses) != 1:
        detail = '、'.join(sorted(addresses)) or '无'
        raise RuntimeError('无法唯一确定电脑局域网 IP（候选：' + detail + '）。请用 -HostIP 或 --host-ip 指定。')
    return addresses.pop()


def resolve_host_ip(value='auto'):
    if value and value != 'auto':
        return str(ipaddress.IPv4Address(value))
    script = """
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$rows=@(Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | ForEach-Object {
    Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -AddressState Preferred -ErrorAction SilentlyContinue |
        Select-Object IPAddress
})
ConvertTo-Json -InputObject $rows -Compress
"""
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, encoding='utf-8', timeout=12)
        if result.returncode:
            raise ValueError('Windows interface query failed')
        rows = json.loads(result.stdout.lstrip('\ufeff'))
        if not isinstance(rows, list):
            raise ValueError('Invalid interface query result')
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise RuntimeError('电脑网卡查询失败，请用 -HostIP 或 --host-ip 指定局域网 IPv4。') from exc
    return choose_address(rows)


if __name__ == '__main__':
    try:
        print(resolve_host_ip(sys.argv[1] if len(sys.argv) > 1 else 'auto'))
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
