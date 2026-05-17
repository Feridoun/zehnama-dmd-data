# zehnama-dmd-data

NHS Dictionary of medicines and devices (dm+d) data packs for the [Zehnama EPR](https://github.com/Feridoun/rvh-epr) application.

This repository decouples the dm+d medication reference dataset from the compiled Zehnama binary. The app's `DmdUpdateService` polls the **latest release** of this repo (≈ once every 24 h) and applies new packs without forcing a software upgrade — so users always have a current dm+d without us having to ship a new MSI for every monthly TRUD release.

## Pack format

Each release MUST attach exactly two assets:

| Asset                          | Purpose                                                                  |
| ------------------------------ | ------------------------------------------------------------------------ |
| `dmd_medications.json`         | The medication pack consumed by the app.                                 |
| `dmd_medications.json.sha256`  | SHA-256 digest of the JSON (sha256sum format: `<hex>  filename`).        |

The **release tag** is the pack version. Use a sortable date format: `YYYY.MM.DD` (e.g. `2026.05.17`). The app uses dotted-numeric comparison to decide whether a release is newer than the installed pack.

### JSON schema

```jsonc
{
  "version": "2026.05.17",
  "lastUpdated": "2026-05-17",
  "description": "NHS dm+d subset, monthly TRUD release",
  "source": "NHS Business Services Authority",
  "medications": [
    {
      "vtmCode": "VTM12345",          // Virtual Therapeutic Moiety id
      "vmpCode": "VMP39211411000001100", // Virtual Medicinal Product id
      "name": "Amoxicillin",          // generic name
      "brandName": "Augmentin",       // optional, AMP brand
      "form": "capsule",              // dose form
      "strength": "500mg",            // strength string
      "route": "oral",                // administration route
      "bnfCode": "5.1.1.3",          // British National Formulary
      "atcCode": "J01CA04",          // optional WHO ATC
      "synonyms": ["amox", "amoxycillin"]
    }
  ]
}
```

The `medications` array MUST be non-empty (the client rejects empty packs). All fields except `medications` are advisory; missing fields are tolerated.

## Threat model & integrity

* **Transport**: HTTPS to `api.github.com` and `objects.githubusercontent.com` (TLS pinning provided by Windows TLS stack).
* **Integrity**: SHA-256 verified on every download. A mismatch aborts the install without overwriting the existing pack.
* **Size cap**: Client refuses to install packs > 50 MB (current dm+d JSON ≈ 5–10 MB).
* **Atomic install**: Pack is written to a temp file, renamed onto the live path; the pointer JSON is also written via temp + rename. A power loss mid-install cannot corrupt the running pack.
* **Single publisher**: This repo is owned by `Feridoun` with branch protection on `main` and required signed commits. Only authorised releases reach the latest endpoint.
* **No PHI**: dm+d is public reference data. The client never transmits patient information during update checks.

### Why not cosign / GPG signatures (today)

For a single-publisher feed served exclusively over HTTPS with branch protection on `main`, SHA-256 + GitHub's immutable release assets adequately defend against the practical threats (transit tampering, asset substitution after publication). Cosign-style detached signatures would defend against a compromised release token but at the cost of separate key management. Deferred until either (a) a second publisher needs to push releases or (b) the app is distributed outside controlled NHS networks. Tracked under the project safety case.

## Building a release

### Prerequisites

1. A TRUD account (free): <https://isd.digital.nhs.uk/trud/users/guest/filters/0/home>.
2. Subscribe to the **NHS dm+d** release.
3. Python 3.10+ (script uses only the standard library; no `pip install` needed).

### Steps

```bash
# 1. Download the monthly dm+d archive from TRUD (a `.zip` of `.xml` files).

# 2. Convert it (the converter accepts the zip directly or an extracted folder).
python scripts/convert_trud_dmd.py path/to/dmd_release.zip --version 2026.05.17 --out dist

# 3. Outputs land in `dist/`:
#    dist/dmd_medications.json
#    dist/dmd_medications.json.sha256

# 4. Create the GitHub release (tag = version).
gh release create 2026.05.17 dist/dmd_medications.json dist/dmd_medications.json.sha256 \
  --title "dm+d 2026.05 (May 2026 TRUD release)" \
  --notes "Monthly NHS BSA dm+d publication. See <release-notes-url>."
```

The `validate-release` workflow runs automatically on tag push and confirms the assets parse, the medications array is non-empty, and the SHA-256 sidecar matches the JSON.

## Licensing & attribution

The NHS dm+d is published by the NHS Business Services Authority and licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) via the [TRUD](https://isd.digital.nhs.uk/trud) service. End users of Zehnama are deemed sub-licensees; the application surfaces this attribution at first launch and in the About dialog.

The scripts and workflow code in this repository are released under the same MIT licence as the Zehnama application.

## Compatibility

| App version | Min pack schema | Notes |
|-------------|-----------------|-------|
| 0.9.0+      | v1 (this doc)   | First release with auto-update. |
