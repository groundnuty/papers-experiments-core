---
paths:
  - "src/expcore/records/**"
  - "src/expcore/replay/**"
  - "tests/data/**"
  - "tests/records/**"
---

# Record versions

- v0.1 has one new record version, `expcore-record/3`. Every branch that needs new record fields extends that same /3; none adds a /4 without the coordinating session's agreement.
- Until v0.1.0 is tagged, every /3 extension is strictly additive: new fields are optional (absent means "not recorded"), and no existing field changes meaning or validation. The first study writes live /3 records before later branches extend /3, and reads them all under the newest code.
- Each /3 merge point adds a fixture record, written by that point's code, to the fixture set that a test replays byte for byte under the current code. An extension that fails it is not additive.
- A rule that depends on the record's version applies to records of the version that introduced it and later, never retroactively: an older record replays under the rules it was written under.
