# Security Policy

**Last Updated**: March 19, 2026  
**Version**: 1.0

---

## 📋 Table of Contents

- [Supported Versions](#supported-versions)
- [Reporting a Vulnerability](#reporting-a-vulnerability)
- [Security Best Practices](#security-best-practices)
- [Known Security Considerations](#known-security-considerations)
- [Secure Configuration](#secure-configuration)
- [Authentication & Authorization](#authentication--authorization)
- [Third-Party Dependencies](#third-party-dependencies)
- [Incident Response](#incident-response)
- [Security Contacts](#security-contacts)

---

## Supported Versions

Security updates and patches are provided for the following versions:

| Version | Release Date | End of Support | Status | Notes |
|---------|--------------|-----------------|--------|-------|
| 1.0.x | 2026-03-19 | 2028-03-19 | ✅ Supported | Current stable release |
| 0.9.x | 2025-12-01 | 2026-03-19 | ⚠️ Limited | Security patches only |
| < 0.9 | N/A | 2025-12-01 | ❌ Unsupported | No longer receiving updates |

**Support Duration**: 24 months from release date for current version, 3 months for previous version.

### Version Recommendation
Users should upgrade to the latest version (1.0.x) to receive all security updates and bug fixes.

---

## Reporting a Vulnerability

### ⚠️ Responsible Disclosure

We take security vulnerabilities seriously. If you discover a security vulnerability, **please do not open a public GitHub issue**. Instead, follow our responsible disclosure process below.

### How to Report

**Step 1: Contact Security Team**
- **Email**: security@example.com
- **Subject Line**: `[SECURITY] Client-Sim Vulnerability Report`
- **Alternative**: Use GitHub's Security Advisory feature (if available)

**Step 2: Include Required Information**
Please provide:
- Description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Impact assessment
- Suggested remediation (if available)
- Your contact information

**Step 3: Example Report Format**
```
Vulnerability Type: [e.g., Privilege Escalation, Command Injection, etc.]
Severity: [Critical/High/Medium/Low]
Affected Versions: [List version(s)]
Discovered By: [Your Name/Organization]
Date Discovered: [Date]

Description:
[Detailed description of the vulnerability]

Steps to Reproduce:
1. [Step 1]
2. [Step 2]
...

Impact:
[Describe potential impact]

Proof of Concept:
[Include if applicable, keep minimal]
```

### Response Timeline

- **Acknowledgment**: Within 24 hours of receiving your report
- **Initial Assessment**: Within 48 hours
- **Updates**: At least every 7 days while investigation is ongoing
- **Resolution**: Target 30 days for critical issues, 90 days for other issues
- **Disclosure**: After patch is released or 90 days, whichever is sooner

### Coordination Process

1. **Days 1-2**: Initial acknowledgment and triage
2. **Days 3-7**: Vulnerability assessment and reproduction confirmation
3. **Days 8-30**: Development of fix and security testing
4. **Days 31+**: Release coordination and public disclosure preparation
5. **Day ~45**: Security advisory published, patch released

### Public Disclosure

- We will credit the researcher in security advisories (unless requested otherwise)
- The disclosure date will be coordinated with the researcher
- A CVE identifier will be requested if severity warrants

### Out of Scope

The following are generally out of scope:
- Social engineering attacks
- Phishing attempts
- DDoS attacks
- Performance-based DoS attacks
- Physical security vulnerabilities
- Issues in third-party dependencies (report to maintainers directly)

---

## Security Best Practices

### For Users

#### 1. Installation Security
```bash
# ✅ DO: Verify downloads
sha256sum client-sim.tar.gz
# Compare with published hash

# ❌ DON'T: Use untrusted sources
# Don't download from unofficial mirrors
```

#### 2. Privilege Management
```bash
# ✅ DO: Use sudo only when necessary
sudo /usr/local/scripts/simulation.sh

# ❌ DON'T: Run as root unnecessarily
# Don't grant unnecessary privileges
```

#### 3. Configuration Security
```bash
# ✅ DO: Protect configuration files
sudo chmod 640 /usr/local/scripts/simulation.conf

# ✅ DO: Use strong WiFi passwords in config
ssidpw=GeneratedStrongPassword123!

# ❌ DON'T: Use default passwords
# ❌ DON'T: Share configuration files with credentials
```

#### 4. Network Security
```bash
# ✅ DO: Use firewall rules
sudo ufw allow 5201:5210/tcp  # iperf ports

# ✅ DO: Validate DNS servers
# Use known-good DNS servers in configuration

# ❌ DON'T: Allow public access to simulation servers
```

#### 5. Regular Updates
```bash
# ✅ DO: Keep scripts updated
cd /usr/local/scripts
git pull origin main

# ✅ DO: Monitor for security advisories
# Subscribe to project notifications

# ❌ DON'T: Skip version updates
```

### For Developers

#### 1. Code Security
- **Input Validation**: Always validate configuration inputs
- **Command Injection**: Use array syntax, avoid eval
- **Privilege Escalation**: Minimize sudo usage
- **Secrets**: Never hardcode credentials

#### 2. Script Security
```bash
# ✅ DO: Use safe variable expansion
"${variable}"

# ✅ DO: Validate file paths
if [[ -f "$file_path" ]]; then

# ❌ DON'T: Use eval with user input
# ❌ DON'T: Use unquoted variables
```

#### 3. Dependency Security
- Keep dependencies up to date
- Use dependency scanning tools
- Monitor CVE databases
- Pin versions for reproducibility

#### 4. Testing Security
```bash
# Test with multiple user privilege levels
# Test with invalid configurations
# Test with malformed input
# Test with restricted file permissions
```

---

## Known Security Considerations

### 1. Privilege Requirements

**Current Design**:
- Linux: Requires `sudo` for network operations (WiFi, interfaces)
- Windows: Requires Administrator for network operations
- Some operations require elevated privileges

**Recommendation**: 
- Run in isolated environments where possible
- Use dedicated accounts for simulation purposes
- Apply principle of least privilege

### 2. Network Operations

**Risk**: Scripts perform network reconfigurations
**Mitigation**:
- Run in test/lab environments
- Validate network changes before automation
- Implement change logs for audit trails
- Use network isolation/VLANs in production

### 3. Configuration File Access

**Risk**: Configuration files may contain sensitive information
**Mitigation**:
```bash
# Restrict file permissions
sudo chmod 640 /usr/local/scripts/simulation.conf

# Remove credentials before sharing
# Use environment variables for secrets
```

### 4. Logging

**Risk**: Simulation logs may contain network information
**Mitigation**:
- Store logs in secure locations
- Implement log rotation
- Restrict log file access
- Audit log access

### 5. Third-Party Dependencies

**Risk**: External tools may have vulnerabilities
**Mitigation**:
- Keep tools updated (iperf3, git, wget, etc.)
- Verify tool integrity before installation
- Monitor CVE databases
- Use official package repositories

---

## Secure Configuration

### Recommended Security Configuration

```ini
# /usr/local/scripts/simulation.conf

[simulation]
# Only enable necessary simulations
kill_switch=on                  # Default to OFF for safety
rapid_update=on                 # Keep scripts updated
sim_load=50                     # Reasonable load

# Use known-good repositories
github_repo=on
repo_location=https://github.com/solutions-hpe/client-sim.git
repo_branch=main

[address]
# Use verified, secure DNS servers
dns_latency_1=8.8.8.8           # Google DNS (verified)
dns_latency_2=8.8.4.4           # Google DNS secondary
dns_latency_3=1.1.1.1           # Cloudflare DNS

# Use real server addresses, not test addresses
ping_address=8.8.8.8
iperf_server=192.168.1.50       # Verified iperf server

# Secure SMB configuration
syslog_server=192.168.1.100     # Encrypted, firewalled
```

### File Permissions

```bash
# Configuration files
sudo chmod 640 /usr/local/scripts/*.conf

# Scripts
sudo chmod 750 /usr/local/scripts/*.sh

# Logs
sudo chmod 640 /usr/local/scripts/*.log

# Sensitive files
sudo chown root:root /usr/local/scripts/simulation.conf
sudo chmod 600 /usr/local/scripts/simulation.conf
```

### Firewall Rules

```bash
# Linux - Only allow necessary traffic
sudo ufw allow 5201:5210/tcp    # iperf3 ports
sudo ufw allow 53/udp           # DNS
sudo ufw allow 123/udp          # NTP
sudo ufw deny incoming          # Default deny

# Restrict SSH if used for updates
sudo ufw allow from 192.168.1.0/24 to any port 22
```

---

## Authentication & Authorization

### User Access Control

**Linux**:
```bash
# Create dedicated user for simulations
sudo useradd -m -s /bin/bash sim_user

# Grant sudo for specific commands only
sudo visudo  # Add: sim_user ALL=(ALL) NOPASSWD: /usr/local/scripts/simulation.sh
```

**Windows**:
```powershell
# Create dedicated service account
New-LocalUser -Name "SimUser" -Description "Simulation Service"

# Add to local admin group if necessary
Add-LocalGroupMember -Group "Administrators" -Member "SimUser"
```

### Credential Management

✅ **DO**:
- Store credentials in environment variables
- Use Windows Credential Manager / Linux keyrings
- Rotate passwords regularly
- Use service accounts with minimal privileges

❌ **DON'T**:
- Hardcode passwords in scripts
- Store credentials in configuration files
- Share credential files
- Use shared service accounts

### Multi-User Environments

For multi-user setups:
```bash
# Create separate user accounts
# Restrict access via file permissions
sudo chmod 700 /home/sim_user/.ssh

# Audit access logs
sudo tail -f /var/log/auth.log
```

---

## Third-Party Dependencies

### Required Dependencies & Security Status

#### Linux
| Package | Version | Security Status | Update Check |
|---------|---------|-----------------|--------------|
| git | Latest | ✅ Active | `git --version` |
| wget | Latest | ✅ Active | `wget --version` |
| iPerf3 | 3.11+ | ✅ Active | `iperf3 --version` |
| network-manager | Latest | ✅ Active | `nmcli --version` |
| rsyslog | Latest | ✅ Active | `rsyslogd -v` |

#### Windows
| Package | Version | Security Status | Update Check |
|---------|---------|-----------------|--------------|
| PowerShell | 5.0+ | ✅ Active | `$PSVersionTable` |
| Git | Latest | ✅ Active | `git --version` |
| iperf3 | 3.11+ | ✅ Active | `iperf3 --version` |

### Vulnerability Monitoring

Monitor for CVEs in:
- **Linux**: Package manager advisories (`apt list --upgradable`)
- **Windows**: Windows Update, Patch Tuesday
- **Tools**: Project CVE databases
  - iperf3: https://github.com/esnet/iperf/security
  - Git: https://github.com/git/git/security/advisories
  - PowerShell: https://github.com/PowerShell/PowerShell/security

### Update Procedures

```bash
# Linux - Keep all packages current
sudo apt update
sudo apt upgrade -y

# Windows - Enable automatic updates
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned
Update-Help -Force
```

---

## Incident Response

### Security Incident Process

**If you discover a security issue:**

1. **Isolate** - Stop affected systems if possible
2. **Document** - Record what happened and timeline
3. **Assess** - Determine scope and impact
4. **Report** - Contact security team immediately
5. **Preserve** - Keep logs and evidence for investigation
6. **Communicate** - Follow coordination process

### Incident Report Template

```
Incident Type: [Type of security incident]
Date/Time Detected: [When discovered]
Date/Time Reported: [When reported]
Systems Affected: [List of systems]
Severity: [Critical/High/Medium/Low]

Timeline:
[Detailed timeline of events]

Impact Assessment:
[Who/what was affected]

Containment Actions Taken:
[Actions taken to prevent spread]

Root Cause:
[Analysis of how it happened]

Remediation:
[Steps to fix and prevent recurrence]
```

---

## Security Contacts

### Reporting Security Issues

- **Email**: security@example.com
- **PGP Key**: Available upon request
- **Response Time**: 24 hours for acknowledgment

### Security Updates

- **Subscribe**: Watch repository for security advisories
- **Mailing List**: (Coming soon) Security notifications list
- **RSS Feed**: GitHub releases for security patches

---

## Security Changelog

### Version 1.0 (Current)
- ✅ Security policy implemented
- ✅ Responsible disclosure process documented
- ✅ Best practices guide created
- ✅ Vulnerability handling procedures established

### Future Security Enhancements
- [ ] Implement code signing for releases
- [ ] Create security scanning CI/CD pipeline
- [ ] Add security testing framework
- [ ] Establish bug bounty program

---

## Compliance & Standards

This project follows security best practices from:
- **CWE**: Common Weakness Enumeration
- **OWASP**: Open Web Application Security Project
- **NIST**: National Institute of Standards and Technology
- **IEEE**: Institute of Electrical and Electronics Engineers

---

## Additional Resources

- 🔒 [OWASP Security Guidelines](https://owasp.org/)
- 🛡️ [CWE Top 25](https://cwe.mitre.org/top25/)
- 📋 [NIST Cybersecurity Framework](https://www.nist.gov/cyberframework)
- 🔑 [Linux Security Best Practices](https://wiki.debian.org/SecureApt)
- 🪟 [Windows Security Baseline](https://learn.microsoft.com/en-us/windows/security/benchmark/)

---

## Questions or Concerns?

For security-related questions or concerns:
1. Email: security@example.com
2. Create a private security advisory on GitHub
3. Check existing security advisories

---

**Last Updated**: March 19, 2026  
**Maintained By**: GitHub Copilot  
**Status**: Active & Current ✅
