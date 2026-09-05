# SAP LLD Generator

An AI-assisted Low-Level Design (LLD) tool for a custom ABAP/ECC codebase. Given a
business requirement, it retrieves the code most likely relevant to it from a
structural dependency graph and an embedded vector store, so a later LLM-reasoning
step can propose an anchored change.

## Layout

- **[`Step 1/`](Step%201/)** — ABAP side: `ZLLD_PACKAGE_EXTRACTOR`, a new SAP report
  that extracts a package's objects (source + dependency + DDIC metadata) on first
  run and syncs only what changed via released transports on every run after. Runs
  inside an SAP system; not part of the Python pipeline below. See
  [`Step 1/LLD_EXTRACTOR_DESIGN_NOTES.md`](Step%201/LLD_EXTRACTOR_DESIGN_NOTES.md)
  for what it does, what's still unverified against a real system, and the
  acceptance-criteria test plan.
- **[`sap_lld_step2/`](sap_lld_step2/)** — Python side: parses the extraction file
  Step 1 produces (or a mock/synthetic one for now), builds the structural graph
  and embedding store (step 2), and runs retrieval + tiering against a business
  requirement (step 3). See [`sap_lld_step2/README.md`](sap_lld_step2/README.md)
  for setup, running the pipeline, and configuration.

## Status

- **Step 1** (ABAP extraction report): built, not yet verified against a real SAP
  system — see its design notes for what to check first.
- **Step 2** (graph + embedding store): built and validated against both a
  synthetic mock file and a realistic hand-crafted extraction file
  (`ZORDER_MGMT_extract.txt`).
- **Step 3** (retrieval + tiering): built and tested against the same realistic
  file, including a scoring-formula bug found and fixed during testing (see
  `sap_lld_step2/reports/`).
- **Step 4** (LLM reasoning via a Gemini gem): not started.

Step 2's parser currently expects the simplified extraction format used by its own
mock generator, not Step 1's real, richer output — that update is separate,
not-yet-built work (see `sap_lld_step2/README.md` and Step 1's design notes for
this hand-off gap).
