# Version Management

> **Historical note (2026-07):** This document captures the **pre-reset
> standalone client-sim scripts** era and its `MAJOR.MINOR` per-script
> versioning scheme. The repo has since moved to an **autobumped `VERSION`
> file** (`.NN` scheme — a `version-bump.yml` bot handles the last segment;
> never bump manually) shared across the LM fleet; the per-script version
> lines below no longer track the live `clients/linux/*.sh` / `clients/windows/*.ps1`
> files. Read this as a snapshot of the legacy scripts' version history, not a
> current source of truth. The canonical repo version is the root `VERSION` file.
> **`vhconnect.sh` / `vhconnect.ps1` are listed below but do NOT exist in this
> repo** (VirtualHere handling was removed); disregard those two entries.

**Last Updated**: May 16, 2026
**Format**: SemVer-inspired (MAJOR.MINOR format)

---

## 📋 Table of Contents

- [Version Overview](#version-overview)
- [WebUI Dashboard](#webui-dashboard)
- [Linux Scripts](#linux-scripts)
- [Windows Scripts](#windows-scripts)
- [Version History](#version-history)
- [Versioning Guidelines](#versioning-guidelines)
- [Update Process](#update-process)

---

## Version Overview

The Client Simulation Suite uses a distributed versioning system where each script maintains its own version number. This allows for independent updates to individual components while maintaining compatibility with the overall system.

### Version Format
- **Format**: `MAJOR.MINOR` (e.g., `0.91`, `0.02`)
- **MAJOR**: Reserved for breaking changes or major functionality updates
- **MINOR**: Incremented for bug fixes and minor improvements
- **Separation**: Versions are separated by release milestones

### Compatibility Notes
- Linux and Windows versions are kept synchronized for the same script
- Version numbers match between platforms (e.g., apt_update.sh v0.02 ↔ apt_update.ps1 v0.02)
- Cross-platform compatibility is maintained at the same version level

---

## WebUI Dashboard

#### Dashboard / install-lxc.sh
- **Current Version**: 0.38
- **Purpose**: FastAPI dashboard, LXC installer, GitHub sync hub, Aruba Central monitoring, and notification workflows
- **Status**: Stable
- **Last Updated**: May 16, 2026

**Major Version Milestones:**
- **v0.33-v0.35**: Initial WebUI setup, Aruba Central integration, simulation tiles, hardware alert tiles, and client count monitoring
- **v0.36**: Notifications UI (email SMTP + Teams webhook), configurable GitHub sync interval, sim site → client drill-down with SIM/ALERT indicators, sudoers wildcard fix for self-update, and `nm-applet` suppression
- **v0.37**: Hardware Alerts Setup UI in the Setup tab, self-update `SIGTERM` fix (`start_new_session=True`), and clients table UX cleanup
- **v0.38**: Monitored Central Checks added as the fourth Simulations tab section, with `renderChecksList()` triggered on settings updates

---

## Linux Scripts

### Core Simulation Scripts

#### 1. simulation.sh
- **Current Version**: 0.91
- **Purpose**: Main simulation orchestration
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Key Features**:
  - 4 helper functions (apply_override, connect_wifi, manage_connection, run_simulation)
  - 15% code optimization
  - 3 critical bugs fixed

#### 2. startup.sh
- **Current Version**: 0.33
- **Purpose**: System startup and initialization
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Key Features**:
  - Syslog configuration
  - Interface setup
  - Update scheduling

#### 3. vhconnect.sh
- **Current Version**: 0.18
- **Purpose**: VirtualHere device connection management
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Key Features**:
  - Device list management
  - Cached device connection
  - Fallback mechanisms

#### 4. update.sh
- **Current Version**: 0.21
- **Purpose**: Script and configuration updates
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Key Features**:
  - Git-based updates
  - SMB backup location support
  - Automatic file distribution

### Simulation Scripts

#### 5. sys_mon.sh
- **Current Version**: 0.06
- **Purpose**: System monitoring and reboot triggers
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Key Features**:
  - Log monitoring
  - Automatic reboot on failures
  - Event detection

#### 6. apt_update.sh
- **Current Version**: 0.02
- **Purpose**: Package management and system updates
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Optimizations**: 46% code reduction
- **Key Features**:
  - Consolidated package installation
  - System updates
  - Dependency management

#### 7. dns_fail.sh
- **Current Version**: 0.01
- **Purpose**: DNS failure simulation
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Optimizations**: 20% code reduction
- **Key Features**:
  - Multiple bad DNS server simulation
  - Latency injection
  - Query logging

#### 8. download.sh
- **Current Version**: 0.01
- **Purpose**: Download simulation with random file selection
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Optimizations**: 22% code reduction
- **Key Features**:
  - Random file selection
  - Download statistics
  - Progress logging

#### 9. iperf.sh
- **Current Version**: 0.01
- **Purpose**: Bandwidth testing
- **Status**: Stable
- **Last Updated**: March 19, 2026
- **Optimizations**: 32% code reduction
- **Key Features**:
  - Multi-port testing
  - Bandwidth limits
  - Performance metrics

### Utility Scripts

#### 10. ini-parser.sh
- **Current Version**: Not versioned (Library)
- **Purpose**: Configuration file parsing
- **Status**: Production utility
- **Last Updated**: Not modified
- **Key Features**:
  - INI format parsing
  - Section management
  - Key-value extraction

---

## Windows Scripts

### Core Simulation Scripts

#### 1. simulation.ps1
- **Current Version**: 0.91
- **Purpose**: Main simulation orchestration (PowerShell)
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Platform**: Windows PowerShell 5.0+
- **Equivalent To**: linux/simulation.sh v0.91
- **Key Features**:
  - Full feature parity with Linux version
  - Event Log integration
  - Scheduled Tasks support

#### 2. startup.ps1
- **Current Version**: 0.33
- **Purpose**: Windows startup and configuration
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Platform**: Windows with Admin privileges
- **Equivalent To**: linux/startup.sh v0.33
- **Key Features**:
  - Event Forwarding setup
  - Network adapter configuration
  - Service initialization

#### 3. vhconnect.ps1
- **Current Version**: 0.18
- **Purpose**: VirtualHere device management (Windows)
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Equivalent To**: linux/vhconnect.sh v0.18
- **Key Features**:
  - Device enumeration
  - Connection management
  - Error handling

#### 4. update.ps1
- **Current Version**: 0.21
- **Purpose**: Script updates via Git/SMB (Windows)
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Equivalent To**: linux/update.sh v0.21
- **Key Features**:
  - Git integration
  - SMB support
  - Script distribution

### Simulation Scripts

#### 5. sys_mon.ps1
- **Current Version**: 0.06
- **Purpose**: System monitoring via Event Log (Windows)
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Equivalent To**: linux/sys_mon.sh v0.06
- **Key Features**:
  - Event Log monitoring
  - Automatic restart
  - Error detection

#### 6. apt_update.ps1
- **Current Version**: 0.02
- **Purpose**: Package management (Windows/winget)
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Equivalent To**: linux/apt_update.sh v0.02
- **Key Features**:
  - winget integration
  - Package updates
  - Dependency management

#### 7. dns_fail.ps1
- **Current Version**: 0.01
- **Purpose**: DNS failure simulation (PowerShell)
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Equivalent To**: linux/dns_fail.sh v0.01
- **Key Features**:
  - Resolve-DnsName queries
  - Bad DNS simulation
  - Query logging

#### 8. download.ps1
- **Current Version**: 0.01
- **Purpose**: Download simulation (Windows)
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Equivalent To**: linux/download.sh v0.01
- **Key Features**:
  - Invoke-WebRequest integration
  - Random file selection
  - Progress tracking

#### 9. iperf.ps1
- **Current Version**: 0.01
- **Purpose**: Bandwidth testing (Windows)
- **Status**: Production Ready
- **Created**: March 19, 2026
- **Equivalent To**: linux/iperf.sh v0.01
- **Key Features**:
  - iperf3.exe execution
  - Multi-port testing
  - Performance analysis

### Utility Scripts

#### 10. ini-parser.ps1
- **Current Version**: Not versioned (Library)
- **Purpose**: Configuration parsing (PowerShell)
- **Status**: Production utility
- **Created**: March 19, 2026
- **Platform**: PowerShell 5.0+
- **Key Features**:
  - INI file parsing
  - Hashtable creation
  - Config management

---

## Version Summary Table

| Script | Linux Version | Windows Version | Status | Platform Sync |
|--------|---------------|-----------------|--------|---------------|
| simulation | 0.91 | 0.91 | ✅ | In Sync |
| startup | 0.33 | 0.33 | ✅ | In Sync |
| vhconnect | 0.18 | 0.18 | ✅ | In Sync |
| update | 0.21 | 0.21 | ✅ | In Sync |
| sys_mon | 0.06 | 0.06 | ✅ | In Sync |
| apt_update | 0.02 | 0.02 | ✅ | In Sync |
| dns_fail | 0.01 | 0.01 | ✅ | In Sync |
| download | 0.01 | 0.01 | ✅ | In Sync |
| iperf | 0.01 | 0.01 | ✅ | In Sync |
| ini-parser | Library | Library | ✅ | N/A |

---

## Version History

### Linux Scripts

```
simulation.sh
├── v0.91 (2026-03-19) - Optimization release
│   ├── 4 helper functions added
│   ├── 15% code reduction
│   ├── 3 bugs fixed
│   └── 10-20% performance improvement
└── Earlier versions (not documented)

startup.sh
├── v0.33 (2026-03-19) - Current
├── v0.32 (Unknown)
└── Earlier versions (not documented)

vhconnect.sh
├── v0.18 (2026-03-19) - Current
└── Earlier versions (not documented)

update.sh
├── v0.21 (2026-03-19) - Current
└── Earlier versions (not documented)

sys_mon.sh
├── v0.06 (2026-03-19) - Current
└── Earlier versions (not documented)

apt_update.sh
├── v0.02 (2026-03-19) - Optimized
│   └── 46% code reduction
└── v0.01 (Earlier)

dns_fail.sh
├── v0.01 (2026-03-19) - Optimized
│   └── 20% code reduction
└── Earlier versions

download.sh
├── v0.01 (2026-03-19) - Optimized
│   └── 22% code reduction
└── Earlier versions

iperf.sh
├── v0.01 (2026-03-19) - Optimized
│   └── 32% code reduction
└── Earlier versions
```

### Windows Scripts

All Windows PowerShell scripts were created on 2026-03-19 with version parity to Linux versions:

```
All PowerShell Scripts (*.ps1)
├── Created: 2026-03-19
├── Status: Production Ready
├── Platform: Windows 10+, Windows Server 2016+
├── PowerShell: 5.0+
└── Feature Parity: 100% with Linux equivalents
```

---

## Versioning Guidelines

### Version Increment Rules

**MAJOR Version (First digit)**
- Reserved for breaking changes
- Major feature additions
- Significant API changes
- Currently unused (all at 0.x)

**MINOR Version (Decimal portion)**
- Bug fixes and improvements
- Performance optimizations
- Minor feature additions
- Security patches

### Update Frequency
- **Critical bugs**: Immediate patch
- **Security issues**: Within 30 days (critical) or 90 days (other)
- **Feature updates**: Monthly or quarterly
- **Minor improvements**: Ad-hoc

### Version Coordination

**Windows-Linux Sync**:
- Major feature updates synchronized between platforms
- Version numbers match for equivalent functionality
- Platform-specific optimizations don't increment version
- Both platforms move to same version simultaneously

---

## Update Process

### Updating a Script Version

**In Bash Scripts** (.sh):
```bash
# Find version line
grep "^version=" script.sh

# Update version
sed -i 's/version=.XX/version=.YY/' script.sh
```

**In PowerShell Scripts** (.ps1):
```powershell
# Find version line
Select-String 'version = ' script.ps1

# Update version
(Get-Content script.ps1) -replace '\$version = "0\.XX"', '$version = "0.YY"' | Set-Content script.ps1
```

### Documentation Updates

When updating a script version:
1. Update the version in the script file
2. Update this version.md file
3. Update the CHANGELOG (if exists)
4. Create or update security advisory (if needed)
5. Update version in documentation

---

## Future Version Planning

### Planned Milestones

**v1.0.0** (Target: Q3 2026)
- Mature feature set
- Comprehensive testing
- Production-grade stability
- Full documentation
- Security hardening

**v2.0.0** (Future Planning)
- Potential breaking changes
- New simulation types
- Enhanced capabilities
- Modernized codebase

---

## Getting Version Information

### From Linux Scripts

```bash
# Extract version from any .sh file
grep "^version=" /usr/local/scripts/simulation.sh
# Output: version=.91

# Or run script to see version in output
/usr/local/scripts/simulation.sh 2>&1 | grep "Version"
# Output: Simulation Script Version .91
```

### From Windows Scripts

```powershell
# Extract version from any .ps1 file
Select-String 'version = ' C:\Scripts\simulation.ps1

# Or run script to see version
& C:\Scripts\simulation.ps1 2>&1 | Select-String "Version"
```

---

## Support & Maintenance

### Version Support Policy

- **Current Version**: 0.x series (development/stable)
- **Support Window**: 24 months from release
- **Security Patches**: Within 30-90 days
- **EOL Process**: 3-month deprecation notice

### Reporting Version Issues

- Found a bug? Report with version number
- Version mismatch? Check both platforms match
- Update problems? Reference current version

---

## Quick Reference

### Newest Versions (as of March 19, 2026)

| Component | Version |
|-----------|---------|
| simulation | 0.91 |
| startup | 0.33 |
| vhconnect | 0.18 |
| update | 0.21 |
| sys_mon | 0.06 |
| apt_update | 0.02 |
| dns_fail | 0.01 |
| download | 0.01 |
| iperf | 0.01 |

### Version Locations

**Linux**:
- Files: `/usr/local/scripts/*.sh` (line 2)
- Check: `grep "^version=" /usr/local/scripts/script.sh`

**Windows**:
- Files: `C:\Scripts\*.ps1` (line 2-4)
- Check: `Select-String 'version = ' C:\Scripts\script.ps1`

---

**Last Updated**: May 16, 2026
**Maintained By**: GitHub Copilot
**Status**: Active & Current ✅

