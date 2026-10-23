# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue or pull request for an unfixed vulnerability.

Include the affected tool (directory), the commit you tested, and steps or an
input that reproduces the problem. We aim to acknowledge reports within a week.
This is a volunteer-run workshop, so there is no bounty and no guaranteed fix
timeline; we will keep you updated and credit you in the fix unless you ask
otherwise.

## Supported versions

Only the current `main` branch is supported. Fixes are not backported.

## Scope

Every tool in this repository is in scope. Note that several are explicitly
educational prototypes or simulators (see each tool's README); a report is
still welcome, but behaviour the README already documents as unsupported is
not a vulnerability.

## Supply chain

GitHub Actions are pinned to full commit SHAs, Python dependencies are pinned
to exact versions (and hash-pinned where a tool has runtime dependencies),
Dependabot proposes updates after a 7-day cooldown, and CodeQL scans the
Python and JavaScript code on every pull request.
