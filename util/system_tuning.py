#!/usr/bin/env python3
"""
HeIMDALL DAQ System Tuning Utility
Configures memory management and system parameters for optimal real-time performance

Project: HeIMDALL DAQ Firmware
License: GNU GPL V3
Author: Generated via Claude Code optimization plan
"""

import os
import sys
import subprocess
import psutil
import argparse
from pathlib import Path

def check_root():
    """Verify running as root for system modifications"""
    if os.geteuid() != 0:
        print("System tuning requires root privileges. Run with sudo.")
        sys.exit(1)

def configure_huge_pages():
    """Enable transparent huge pages for large shared memory buffers"""
    print("Configuring huge pages...")

    try:
        # Enable transparent huge pages
        thp_path = Path('/sys/kernel/mm/transparent_hugepage/enabled')
        if thp_path.exists():
            thp_path.write_text('always')
            print("  Enabled transparent huge pages")

        # Reserve huge pages for shared memory (estimate: 64MB total)
        total_mem_gb = psutil.virtual_memory().total / (1024**3)
        if total_mem_gb >= 4:  # Only on systems with 4GB+ RAM
            nr_hugepages = min(32, int(total_mem_gb))  # 2MB pages
            hugepages_path = Path('/proc/sys/vm/nr_hugepages')
            if hugepages_path.exists():
                hugepages_path.write_text(str(nr_hugepages))
                print(f"  Reserved {nr_hugepages} huge pages ({nr_hugepages * 2}MB)")
    except Exception as e:
        print(f"  Warning: Huge page configuration failed: {e}")

def configure_memory_locking():
    """Configure memory locking limits"""
    print("Configuring memory locking limits...")

    limits_conf = Path('/etc/security/limits.conf')
    audio_group_rules = [
        "@audio - rtprio 95",
        "@audio - memlock unlimited",
        "@audio - nice -19"
    ]

    try:
        if limits_conf.exists():
            current_content = limits_conf.read_text()

            # Add rules if not present
            for rule in audio_group_rules:
                if rule not in current_content:
                    with limits_conf.open('a') as f:
                        f.write(f"\n{rule}")
                    print(f"  Added: {rule}")
                else:
                    print(f"  Already present: {rule}")
    except Exception as e:
        print(f"  Warning: Memory locking configuration failed: {e}")

def disable_swap():
    """Disable swap for real-time performance"""
    print("Disabling swap...")
    try:
        subprocess.run(['swapoff', '-a'], check=False)
        print("  Swap disabled")

        # Comment out swap entries in fstab
        fstab_path = Path('/etc/fstab')
        if fstab_path.exists():
            lines = fstab_path.read_text().splitlines()
            modified = False
            for i, line in enumerate(lines):
                if 'swap' in line and not line.strip().startswith('#'):
                    lines[i] = f"# {line}  # Disabled for HeIMDALL real-time performance"
                    modified = True

            if modified:
                fstab_path.write_text('\n'.join(lines) + '\n')
                print("  Commented swap entries in /etc/fstab")
    except Exception as e:
        print(f"  Warning: Swap disable failed: {e}")

def configure_cpu_isolation():
    """Configure CPU isolation for real-time cores"""
    print("Checking CPU isolation configuration...")

    try:
        # Check if isolcpus is already set
        cmdline_path = Path('/proc/cmdline')
        if cmdline_path.exists():
            cmdline = cmdline_path.read_text()
            if 'isolcpus' in cmdline:
                print("  CPU isolation already configured")
                return

        # Suggest isolation configuration
        num_cores = psutil.cpu_count()
        if num_cores >= 4:
            isolate_cores = f"1,2,3" if num_cores == 4 else f"1-{num_cores-2}"
            print(f"  To isolate cores {isolate_cores} for real-time work:")
            print(f"  Add 'isolcpus={isolate_cores}' to GRUB_CMDLINE_LINUX in /etc/default/grub")
            print(f"  Then run: sudo update-grub && sudo reboot")
        else:
            print(f"  System has only {num_cores} cores, isolation not recommended")
    except Exception as e:
        print(f"  Warning: CPU isolation check failed: {e}")

def optimize_irq_balance():
    """Disable irqbalance for manual IRQ affinity control"""
    print("Configuring IRQ balance...")

    try:
        # Stop and disable irqbalance service
        subprocess.run(['systemctl', 'stop', 'irqbalance'], check=False)
        subprocess.run(['systemctl', 'disable', 'irqbalance'], check=False)
        print("  Disabled irqbalance service for manual IRQ control")
    except Exception as e:
        print(f"  Warning: IRQ balance configuration failed: {e}")

def apply_sysctl_config():
    """Apply kernel tuning parameters"""
    print("Applying kernel tuning parameters...")

    config_file = Path(__file__).parent / 'kernel_tuning.conf'
    if not config_file.exists():
        print(f"  Warning: {config_file} not found")
        return

    try:
        # Copy to sysctl directory
        target = Path('/etc/sysctl.d/99-heimdall-rt.conf')
        target.write_text(config_file.read_text())

        # Apply immediately
        subprocess.run(['sysctl', '-p', str(target)], check=True)
        print("  Applied kernel tuning parameters")
    except Exception as e:
        print(f"  Warning: Sysctl configuration failed: {e}")

def show_status():
    """Display current system optimization status"""
    print("\nSystem Optimization Status:")
    print("=" * 40)

    # Memory info
    mem = psutil.virtual_memory()
    print(f"Total RAM: {mem.total / (1024**3):.1f}GB")
    print(f"Available: {mem.available / (1024**3):.1f}GB")

    # CPU info
    print(f"CPU cores: {psutil.cpu_count()}")
    print(f"CPU frequency: {psutil.cpu_freq().current:.0f}MHz")

    # Huge pages
    try:
        hugepages = Path('/proc/meminfo').read_text()
        for line in hugepages.splitlines():
            if 'HugePages_Total' in line:
                print(f"Huge pages: {line.split()[-1]}")
                break
    except:
        print("Huge pages: Unknown")

    # Swap status
    swap = psutil.swap_memory()
    print(f"Swap usage: {swap.used / (1024**2):.0f}MB / {swap.total / (1024**2):.0f}MB")

    # RT scheduler status
    try:
        rt_runtime = Path('/proc/sys/kernel/sched_rt_runtime_us').read_text().strip()
        print(f"RT runtime: {'Unlimited' if rt_runtime == '-1' else rt_runtime + 'μs'}")
    except:
        print("RT runtime: Unknown")

def main():
    parser = argparse.ArgumentParser(description='HeIMDALL DAQ System Tuning Utility')
    parser.add_argument('--status', action='store_true', help='Show optimization status only')
    parser.add_argument('--minimal', action='store_true', help='Apply minimal optimizations')
    parser.add_argument('--full', action='store_true', help='Apply full optimizations')

    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if not args.minimal and not args.full:
        print("HeIMDALL DAQ System Tuning Utility")
        print("Use --minimal or --full to apply optimizations, --status to check current state")
        return

    check_root()

    print("HeIMDALL DAQ System Optimization")
    print("=" * 35)

    # Always apply these
    apply_sysctl_config()
    configure_memory_locking()
    optimize_irq_balance()

    if args.full:
        configure_huge_pages()
        disable_swap()
        configure_cpu_isolation()

    print("\nOptimization complete!")
    print("Reboot recommended to ensure all changes take effect.")

    show_status()

if __name__ == '__main__':
    main()