# Standards attribution

OWASP Foundation and contributors publish OWASP Top 10:2025 and OWASP ASVS 5.0.0 under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The adapted standards JSON data retain that license. No OWASP endorsement or certification is implied.

## Sources and changes

- Top 10 overview: https://top10.owasp.org/2025/ (category names as listed in the overview; A09 uses “and”, while its chapter heading uses “&”).
- Official category source files pinned to OWASP/Top10 commit 3a31f35346c3f4e90f350395124382fa53af2afe:
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A01_2025-Broken_Access_Control.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A02_2025-Security_Misconfiguration.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A03_2025-Software_Supply_Chain_Failures.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A04_2025-Cryptographic_Failures.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A05_2025-Injection.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A06_2025-Insecure_Design.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A07_2025-Authentication_Failures.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A08_2025-Software_or_Data_Integrity_Failures.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A09_2025-Security_Logging_and_Alerting_Failures.md
  - https://raw.githubusercontent.com/OWASP/Top10/3a31f35346c3f4e90f350395124382fa53af2afe/2025/docs/en/A10_2025-Mishandling_of_Exceptional_Conditions.md
- ASVS release source: https://raw.githubusercontent.com/OWASP/ASVS/v5.0.0/5.0/docs_en/OWASP_Application_Security_Verification_Standard_5.0.0_en.json
- ASVS tag v5.0.0 resolves to commit 60437a5ed660f757ab9a8d9e0b7181a06492446a.

Changes: selected 152 requirements across all 17 chapters; paraphrased summaries; added path-based applicability hints and editorial cross-standard associations. Requirement identifiers, chapter names, and levels were checked against the release JSON. Top 10 contains all ten categories; CWE names and memberships come from the official mapped-CWE lists (249 unique CWEs). Review lanes follow the local implementation plan. Empty ASVS associations are intentional, not invented matches.

The crosswalk is a review aid, not a claim that a CWE corresponds to one exact ASVS requirement. CWE-918 is officially A01:2025. CWE-829 and CWE-494 are officially A08:2025 even when the affected feature is a supply-chain component. The curated subset is not the entire ASVS and cannot establish ASVS certification.
