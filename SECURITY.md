# Security policy

## Reporting

Please report vulnerabilities privately through the repository's GitHub security
advisory form ("Report a vulnerability"). Do not open public issues for them. We aim to
acknowledge within 5 working days.

In scope: anything that lets a receipt, dataset snapshot, comparison, reproduction
verdict or routing decision misrepresent what happened **without** being detected by
`astrometron verify` / `selfcheck`; credential leakage; unsafe defaults.

## Known limits (by design, V1)

See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). In particular:

- Receipts are not signed. An attacker who can rewrite both the receipt files and
  `index.sqlite` can forge history.
- Configured solvers, scorers, tools and providers are trusted code.
- Hosted model drift behind a stable model name is only visible through what the
  provider reports.

## Secrets

Astrometron refuses literal credentials in eval configs and routing policies. Provide keys
through environment variables named by `api_key_env`. Keys are never written to receipts.
