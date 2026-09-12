# Test inventory

348 tests across 13 files. Run with `python3 -m unittest discover -s tests -t .`

This file is generated: `python3 scripts/make_test_inventory.py`.

| Area | Tests |
|---|---:|
| AI boundary (Claude CLI) | 13 |
| Trust guards (Phase 1) | 37 |
| Persistence | 4 |
| Quotation Extraction Playground | 18 |
| Supplier response test bench | 28 |
| RFQ service flows (Phase 1) | 16 |
| Schema, fields & UI glue (Phase 1) | 31 |
| Document reading & supplier guards (Phase 2) | 30 |
| Price normalisation & line matching (Phase 2) | 27 |
| Supplier service, comparison & FX (Phase 2) | 26 |
| Analyst calculations (Phase 3) | 62 |
| Analyst query & explanation guards (Phase 3) | 29 |
| Analyst service & conversation (Phase 3) | 27 |
| **Total** | **348** |


## AI boundary (Claude CLI)

`tests/test_ai_service.py`

**Claude C L I Provider** (13)

- Argv and environment hygiene
- Factory returns cli provider
- Fast tier uses fast model
- Health reads auth status
- Max turns and dropped connection map to transient
- Missing binary and timeout
- Non json stdout
- Not logged in maps to auth error
- Overloaded is unavailable not a usage limit
- Recorded envelope parses and validates
- Result text fallback when no structured output
- Schema violation is invalid output
- Session limit maps to a usage limit with its reset time


## Trust guards (Phase 1)

`tests/test_guards.py`

**Applicability Guard** (2)

- Missing and unknown are never silently converted to na
- Not applicable requires reason and cannot hit buyer facts

**Completeness** (5)

- Blocking questions are listed without double counting fields
- Scores and readiness
- Status transitions
- Unclassified rfq is capped
- Unknown does not block but conflict and line gaps do

**Evidence** (2)

- Exact and normalised matches
- Prior turn lookup

**Field Update Guard** (10)

- Buyer explicit without evidence is downgraded to recommendation
- Contradiction without correction becomes conflict not a guess
- Explicit correction replaces and keeps history
- Number parsing
- Recommendation cannot overwrite buyer fact
- Registry type wins when the model sends a number as text
- Same value restated adds reference only
- Trailing unit is not duplicated for text fields
- Unknown is preserved and needs evidence
- Verified buyer fact is recorded with provenance

**Label Join** (1)

- Never claims more than it shows

**Line Item Guard** (3)

- Duplicates are not added twice and unverified lines are flagged
- Seven distinct variants become seven lines with stable ids
- Update by line id keeps history

**Quantity Semantics** (5)

- All lines quantified gives reference total with note
- Each quantity recorded at rfq level is not a conflict
- Partial line quantities leave gaps
- Single line mirrors both ways
- Stated total disagreeing with line sum is a conflict

**Question Guard** (8)

- Ai mapped free text answers close questions and unknown marks field
- Conflict generates a required choice question once
- First turn also covers recommended universal fields
- First turn cap and ordering
- Later turn cap is three for ai questions
- Later turns only backfill required fields
- Never reask answered skipped or filled including rephrasings
- Required field never asked gets a standard question once

**Technical Summary** (1)

- Derived only from buyer facts


## Persistence

`tests/test_persistence.py`

**Persistence** (4)

- Init is idempotent and creates parent dirs
- Messages and ai calls with cascade
- Save get list delete
- Survives reopen


## Quotation Extraction Playground

`tests/test_playground.py`

**Adapter** (8)

- An ambiguous requirement does not satisfy a field
- Line items and specifications carry over
- Missing information comes from the existing rules
- Nothing is persisted until the user saves
- Saving produces an rfq phase 2 can load
- Sending an empty result onward is refused
- Several line items get stable sequential ids
- Universal fields are populated with their provenance

**Display** (1)

- A unit already in the value is not repeated

**Extraction** (7)

- A plain email becomes structured requirements
- A source file that was never attached is rejected
- An unreadable attachment with no text is reported
- Attachments are read with the existing reader
- Empty input is refused before any model call
- Evidence is checked against what was actually sent
- Nothing to extract is reported not invented

**Phase1 And2 Untouched** (2)

- Supplier service accepts a playground rfq
- The playground rfq is an ordinary rfq


## Supplier response test bench

`tests/test_testbench.py`

**Clean Run** (4)

- A run writes nothing to the database
- Commercial terms appear once each
- Every extracted value carries the words it came from
- The email body is read as a document

**Deviation** (5)

- A bare certification claim is flagged
- A contradiction is shown with both sides
- A per kilogram price is flagged as not comparable
- A price with no supporting span is flagged
- A quote for a size the rfq does not have is not matched

**Error** (4)

- An empty reply is refused before any model call
- An extraction failure becomes a readable message
- An rfq with no lines is refused
- An unreadable attachment alone is refused

**Merged Issue** (3)

- Different contradictions stay separate
- Line specific issues are never merged
- One contradiction across lines is listed once

**Phase2 Untouched** (1)

- The bench uses the real extractor

**Promote** (2)

- Keeping a run makes it a real supplier response
- Promoting twice does not duplicate

**Provenance** (4)

- A reworded term still cites its own sentence
- A shared number alone does not make a citation
- A value taken from the rfq is not shown as supplier evidence
- A value the supplier never wrote cites nothing

**Segment** (3)

- A decimal price is not cut in half
- A paragraph still splits into sentences
- A row without punctuation is a span of its own

**Unclaimed Figure** (2)

- A figure never extracted is listed
- Figures that were extracted are not listed


## RFQ service flows (Phase 1)

`tests/test_rfq_service.py`

**Service Flow** (16)

- A manual edit is not mistaken for a failed ai turn
- Answers survive ai failure and can be retried
- Dropped connection is retried with the same prompt
- Empty input rejected
- Empty result is allowed for a trivial turn
- Export and listing
- Failed first turn keeps the request and names the rfq to retry
- First turn creates questions persists and audits
- Invalid output is retried with the validation error
- Line item edits from the review editor persist
- Manual edit is buyer fact closes question and is audited
- Placeholder response is rejected and retried
- Recompute refreshes the deterministic layer without an ai call
- Rows without a product are dropped not saved blank
- Seven line items free text turn
- Supplier ready gate and reopen


## Schema, fields & UI glue (Phase 1)

`tests/test_schema.py`

**Answer Collection** (7)

- An answer beats a stray skip tick
- Multi select appends typed detail instead of replacing
- Multi select collects every chosen option
- Nothing selected yields nothing
- Typed pill and skip inputs are all collected
- Typed text overrides a single choice pill
- Widget keys are scoped per turn so answers never leak

**Field Registry** (2)

- New field set all missing
- Registry integrity

**Field Value** (2)

- Display value formats numbers units lists bools
- Status semantics

**Line Item Grid** (8)

- A cell edit is applied to the right row
- A typed bottom row becomes a new line
- An entirely blank added row is ignorable
- Deleted rows are dropped
- Edit add and delete together
- Nan from pandas becomes none
- No changes returns the rows unchanged
- String row indices are handled

**Pending Changes** (4)

- A row merely clicked into is not a change
- Counts each kind of change
- Description is readable
- Nothing pending

**R F Q Helpers** (1)

- Sections and ids

**Schema Round Trip** (3)

- Bad enum values fall back to defaults
- From dict tolerates unknown and missing keys
- Json round trip preserves everything

**Trust Label** (4)

- Conflict shows both values
- Provenance badge never conflates ai with buyer
- Recommended value is shown as a recommendation
- Unknown and missing read differently


## Document reading & supplier guards (Phase 2)

`tests/test_supplier_extraction.py`

**Certification Guard** (4)

- A bare claim is never verified
- A held certificate document verifies the claim
- An expired certificate is marked expired
- Claiming an attachment we do not hold stays claimed

**Conflict And Revision** (3)

- A one sided conflict is not a conflict
- Both sides of a conflict are kept
- The latest response becomes active and earlier ones survive

**Currency Guard** (4)

- A bare dollar sign is ambiguous
- A missing currency blocks comparison
- A subunit without its currency is not a price
- Known codes and unambiguous symbols pass

**Document Extraction** (8)

- Docx numbers paragraphs
- Image extractor does not fake a transcript
- Image transcripts are capped below full confidence
- Missing file is reported
- Pdf pages are separated and not duplicated
- Txt is read directly
- Unknown format is reported not invented
- Xlsx gives cell level locations

**Evidence Guard** (6)

- A fabricated span is rejected
- A price without findable evidence is held for review
- A real span is verified
- Evidence records where it came from
- Punctuation and spacing differences still match
- Unverifiable evidence is stored but flagged

**Invented Price Guard** (2)

- A price absent from the document is flagged
- Quotes for unknown rfq lines are discarded

**Questionnaire Guard** (3)

- A yes is a claim not a verification
- An unanswered item is missing not no
- Answers must map to a real question


## Price normalisation & line matching (Phase 2)

`tests/test_supplier_normalization.py`

**Currency** (1)

- Mixed currencies are not comparable

**Dimension** (1)

- Dimensions are found in varied wording

**Discount** (5)

- An evaluable condition yields an effective price
- An unevaluable condition is not guessed
- An unmet condition leaves the base price
- The condition is never lost
- Threshold parsing

**Lead Time And Validity** (3)

- A range is labelled as an interpretation
- A single figure is taken as stated
- Validity distinguishes a period from a condition

**Matching** (8)

- A model disagreeing with a strong dimension match forces review
- A model id that does not exist is ignored
- An unidentifiable line is left unmatched
- Dimensions match regardless of row order
- Position based matching is capped at probable
- Supplier row number is never assumed to be the rfq line
- Thresholds are ordered
- Two lines claiming one rfq line produce a conflict

**Moq** (3)

- A minimum above the line quantity is flagged not rejected
- A satisfied minimum is not flagged
- Moq is separate from quoted quantity

**Normalization** (5)

- A weight price cannot become a piece price
- An unknown basis is unresolved rather than assumed per piece
- No price is not a zero
- Safe divisions are performed
- The original quote is never destroyed

**Price Basis** (1)

- Reads the basis from the supplier wording


## Supplier service, comparison & FX (Phase 2)

`tests/test_supplier_service.py`

**Comparison Currency** (2)

- An unnamed currency is counted as unconvertible not converted
- Prices convert on request and keep their origin

**Comparison Dataset** (6)

- A silent supplier still appears
- Answering a supplier question records it locally
- Mixed currencies are reported not converted
- One contradiction is listed once not once per line
- Review queue surfaces what cannot be asserted
- Summary is counted from stored data

**Correction** (4)

- A buyer correction becomes active and keeps the original
- A match to a nonexistent line is refused
- An unnamed currency is held out of the price column
- Confirming a match records the decision

**Exchange Rate** (5)

- A failed fetch never invents a rate
- An unknown currency is never converted
- Conversion keeps the original and names the rate
- No rate table means no conversion rather than a guess
- Same currency needs no rate

**Extraction Flow** (7)

- A clean response is extracted matched and persisted
- An omitted line is missing not zero
- Extraction calls are audited
- Invalid output is retried once then recorded as failed
- One supplier failing does not stop the others
- Response type reflects partial coverage
- The matching call is skipped only when dimensions settle every line

**Phase1 Still Works** (1)

- Phase2 tables do not disturb the rfq store

**Revision** (1)

- A later response supersedes the earlier one without deleting it


## Analyst calculations (Phase 3)

`tests/test_analyst_calculations.py`

**Cheapest By Line** (14)

- A basket total is only given when every line has an answer
- A contradiction about lead time keeps the price but says so
- A contradiction about the price excludes it
- A line nobody could quote says so rather than showing a number
- A minimum order above the line quantity makes a quote unusable
- A missing quote is never treated as zero
- Accepting unconfirmed matches is a labelled what if
- An exact tie names both and picks neither
- An unconfirmed line match is excluded until it is confirmed
- An unnamed currency is excluded not guessed
- An unresolved price basis is excluded with its note
- Ignoring minimum orders is a labelled what if
- The answer never recommends or awards
- The lowest comparable price wins each line

**Coverage** (4)

- A supplier who never replied has no coverage and a reason
- Coverage is counted against the lines in scope
- Gaps are typed and never numeric
- Line coverage says who is missing and why

**Currency** (5)

- A converted price names the rate its provider and its date
- A quote with no rate is excluded rather than converted
- One currency needs no conversion at all
- The commonest quote currency is used when the rfq names none
- The rfq currency is used and named as an assumption

**Eligibility** (9)

- A claimed certification is not a verified one
- A document backed certification clears when the rfq names none
- A failed or expired certification never clears even as a what if
- A named requirement must be matched exactly
- A required question left unanswered blocks clearance
- A supplier who never replied is not assessed rather than failed
- Filtering to cleared suppliers explains who it removed
- The qualification rule is always stated
- Treating a claim as proof is marked on the check that was promoted

**Evidence Lookup** (3)

- A certification lookup says a claim is only a claim
- A price lookup returns the stored evidence record
- A term with no recorded span is reported not invented

**Lead Time** (3)

- A contradicted lead time is shown both ways and not ranked
- A range is flagged as an interpretation
- Different starting points raise a comparability warning

**Lookup** (5)

- A price lookup never shows a figure the rules refused
- A response level term appears once per supplier
- A text filter finds the supplier and shows their own wording
- Nothing matching says so rather than returning an empty table
- Rows can be sorted by a shown field

**Moq** (2)

- A minimum order is compared with the line quantity
- An unstated minimum order is not assumed to fit

**Result Shape** (5)

- A what if states the assumption and the real population
- Every answer carries its calculation steps
- Rows survive a round trip through json
- The csv export carries the shown columns
- The result is plain data the session can hold

**Rfq Completeness** (1)

- The percentage carries its numerator denominator and definition

**Same Source Of Truth** (3)

- A cell the comparison calls unresolved is never given a price
- Every price the analyst reports is the comparison cell figure
- The counts agree with the comparison summary

**Unresolved Issues** (2)

- Blocking issues come first
- It gathers review items alongside gaps and constraints

**Validity** (3)

- A conditional validity is never turned into a date
- A fixed period becomes an expiry counted from the received date
- A lapsed quote is warned about

**Why Excluded** (3)

- A supplier that was not excluded is said not to have been
- A valid but dearer quote states the gap to the cheapest
- The reason is given line by line


## Analyst query & explanation guards (Phase 3)

`tests/test_analyst_guards.py`

**Explanation Guard** (5)

- A faithful sentence is kept
- A figure the result does not contain is rejected
- A line number written plainly is still recognised
- An empty or overlong narration is rejected
- Award language is rejected even when the numbers are right

**Line Resolution** (3)

- A line is found however the buyer spells it
- A line this rfq does not have is refused
- Dimensions find the line they describe

**Query Validation** (15)

- A field cannot be listed at a grain it has no meaning at
- A filter the intent does not honour is refused
- A filter value naming a supplier is resolved to its id
- A hypothetical is dropped for an intent that cannot use one
- A lookup field we do not hold is refused rather than guessed
- A price comparison needs one or two suppliers
- A questionnaire key this rfq never asked is refused
- A valid query records how each name was read
- A value that should be a number is checked
- An evidence lookup must say what it wants the source of
- An operator that makes no sense for a field is refused
- An unknown intent is refused
- An unrecognised currency is refused
- Unsupported carries the models own reason
- Why excluded needs exactly one supplier

**Supplier Resolution** (6)

- A letter label is refused with the real names
- A resolution is recorded so the buyer can see how it was read
- An ambiguous name is refused with the candidates
- An id a full name and a distinctive word all resolve
- An unknown supplier is refused and the real names offered
- An unknown what if exclusion is a refusal not a silent drop


## Analyst service & conversation (Phase 3)

`tests/test_analyst_service.py`

**Context Scope** (2)

- A line named in an earlier question does not narrow the next one
- A supplier named in an earlier question does not narrow the next one

**Explanation** (5)

- A faithful narration is kept alongside the summary
- A narration recommending a supplier is dropped
- A narration with an invented figure is dropped
- The answer survives the explanation failing
- Turning narration off costs one model call

**Immutability** (3)

- A hypothetical exclusion leaves the database untouched
- Running a known query needs no model at all
- Treating claims as verified changes the answer not the records

**Persistence** (5)

- A refusal is recorded too
- Both model calls are audited under their own names
- Deleting the rfq takes its analyst history with it
- Each question is recorded with the query it became
- History survives a new service instance

**Refusal** (5)

- A refusal costs no explanation call
- A supplier this rfq never had is refused with the real names
- An empty question is refused before any model call
- An rfq with no extracted responses is refused before any model call
- An unsupported question is refused in the standard words

**Stage A Parsing** (6)

- A fresh question does not inherit the previous what if
- A refinement carries the previous what if forward
- Invalid output is retried once with the validation error
- Several phrasings produce the same answer
- The history it sees carries questions not results
- The planner is never shown a price

**Suggested Question** (1)

- Every suggestion answers without a model call
