# Math Checker — Assistant User Guide

**Who this is for:** anyone who corrects student homework with Math Checker.
**What it covers:** how to run a correction, check the marking, return grades and reports to Google Classroom, and recognise problems.
**The one rule that matters most:** Math Checker assists with marking. It does not replace responsible human verification.

Every page and button name in this guide is written exactly as it appears in the application.

---

## 0. How the system fits together (read once)

- Math Checker runs **on the teacher's computer**. You open it in a browser (the address is usually `http://127.0.0.1:8642`, and the app opens it for you when it starts). It is not a website on the internet unless the teacher has switched on Public Address.
- Your work is stored in a **local database on that computer**. Nothing is uploaded except the answers sent to the AI provider for marking, and the report and grade written back to Google Classroom.
- If the installation has a **Host account**, you will see a sign-in screen first. Use the username and password the teacher gave you. (First-run setup and changing that sign-in live in **Settings → Access & accounts → Host account** — that is an owner task, not an assistant task.)
- Signing in to Math Checker is **not** the same as connecting Google Classroom. Classroom is a separate connection the teacher sets up once in **Google Classroom** or **Settings**.

Two switches decide how much happens by itself:

| Switch | Where | What it means for you |
|---|---|---|
| **Automatic Correction** (a.k.a. automation) | **Automatic Correction** page | Detects, corrects, writes reports and returns grades on a timer without you. |
| **Review mode** | **Automatic Correction** page | While it is ON, every grade is held as a draft. **Nothing is published and nothing is returned until you press Approve.** |

If you are unsure which mode you are in, open **Automatic Correction** and read the two status lines at the top before you touch anything.

---

## Step 1 — Before starting

### 1.1 Confirm the app is working

1. Start Math Checker (the teacher's desktop/Start-menu shortcut, or `MathChecker.bat` in a portable copy).
2. Wait for the browser to open the app. If it does not, open the address printed in the console window yourself.
3. Look at the top-right of the app. The badge tells you the state of the AI provider: it reads **Not configured** when no usable key is available, and **Offline** when the app is in mock/offline mode.
4. Open **Task Manager** and check the four summary tiles: **Correction workers (cap N)**, **Queue**, **Waiting for API**, **Tiny Agent**. A queue that stays above zero with no worker running means the workers are stopped — tell the teacher rather than retrying blindly.

### 1.2 Check the AI provider is available

- **AI** page → **API & providers** tab shows the keys in use. Keys are stored only on this computer and are never shown in full again.
- **Test all keys** runs a small health probe. It uses no student data.
- If the page says no keys are configured, corrections cannot run. Stop and tell the teacher.

### 1.3 Confirm the Google Classroom connection

1. Open **Google Classroom**.
2. **Connect Google Classroom** (only if it is not already connected). **Connection healthy** means it is working.
3. Check that the classes you need are listed and that the account shown is the correct one.
4. If the app reports **Classroom sync failed — previous data kept**, the last successful copy of classes and submissions is still shown. It is safe to keep working on what is there, but tell the teacher.

### 1.4 Synchronise if the data looks old

- **Google Classroom** → **Sync Classroom** (the same button appears on **Assignments**).
- A sync copies classes, assignments and submission statuses. It does **not** mark anything.
- If you only need one assignment refreshed, open it from **Assignments** and use **Refresh**.

### 1.5 Check the assignment and its mark scheme

1. Open **Assignments** and open the assignment you are about to work on.
2. Read the header: title, class, due date and the number of marks.
3. Find the mark scheme line. When it says **Upload the mark scheme** (or "Upload the mark scheme to validate its question structure before correcting"), the assignment is **not ready** — the correction needs the official mark scheme for that exact paper.
4. Confirm the paper is the right one: matching title, chapter/topic, lesson and deadline.

### 1.6 Confirm storage and files

- Open **Storage** and check **Storage status**. It lists classes, assignments, corrections, results, graded (1–9), awaiting review, failed jobs and report links.
- If free space is very low the app reports it in the **Diagnostics self-check**; tell the teacher before starting a large batch.
- The retention setting decides whether original submissions are kept: **Never keep (delete after marking)**, **Until the correction finishes**, **Keep for a number of days**, **Always keep**. You do not need to change it — just be aware that "Never keep" means an original may no longer be on this machine afterwards.

### Before You Start — checklist

- [ ] App is running and I can see the dashboard.
- [ ] The AI provider badge is not showing a problem.
- [ ] Google Classroom shows **Connection healthy** for the correct account.
- [ ] I have synchronised recently (or the data is clearly current).
- [ ] The assignment title, class, due date and mark total are the ones I expect.
- [ ] The official mark scheme for that exact paper is uploaded.
- [ ] There is enough disk space for the number of submissions I am about to process.
- [ ] I know whether **Automatic Correction** and **Review mode** are ON or OFF.

---

## Step 2 — Select the assignment

1. Open **Assignments**. The table shows every assignment with its class, type, students, submitted, checked, review and average.
2. Use the filters to narrow the list: **All**, **Managed by Math Checker**, **External Classroom**, **Local only**.
3. Open the correct assignment and check the statistics strip: **STUDENTS · SUBMITTED · CHECKED · NEEDS REVIEW · OUTSTANDING · AVERAGE**.
4. Read the student table at the bottom of the page: **STUDENT · STATUS · SUBMITTED · MARK · CLASSROOM · CORRECTED**.

### What the statuses mean

| Status you may see | Meaning | What to do |
|---|---|---|
| Submitted / handed in | Work is in and available to correct. | Eligible — correct it. |
| **Opened, not submitted** (draft) | The student has not handed anything in. | Do **not** correct it. Wait, or ask the teacher. |
| Not submitted / missing | No submission exists. | Do **not** invent work; report it. |
| **Corrected** | A result already exists for this submission. | Do not re-run it unless there is a specific reason and the teacher agrees. |
| **late** (shown beside the name) | Handed in after the deadline. | Mark as normal, but mention it if the school penalises lateness. |
| Reclaimed by student | The student took the work back. | Not correctable. |

### Two important warnings

- **External Classroom assignments cannot be published.** If the header says *External Classroom Assignment* and *"Google Classroom only permits the integration that created a CourseWork to change its grades or return it. This assignment was not created by Math Checker, so publishing is not available. You can still correct it and download reports locally."* — then you can correct and download, but the **grade return must be done by hand in Google Classroom**.
- **Classroom total points** is the assignment's real total in Google Classroom and is kept separate from Math Checker's maximum marks. **Update Classroom Assignment** changes Google Classroom itself. Only do that if the teacher has asked you to.

Before you process anything: confirm the **assignment title, chapter/lesson, mark scheme and deadline**. A right correction against a wrong paper is worse than no correction.

---

## Step 3 — Run or monitor the correction

Math Checker corrects in two ways.

### 3.1 Manual correction (one submission, or one batch you start)

1. Open **Correct a PDF**.
2. **Choose both a student submission and the mark scheme PDF.**
3. Press **Queue correction**. The job appears below with its status.
4. Watch the job until it reaches **Completed**.
5. Alternatively, from an assignment: **Upload mark scheme** (if needed) and then **Correct all submitted** (or **Correct Entire Assignment** on the **Google Classroom** page). This queues the submitted work for that assignment.

### 3.2 Automatic correction (the app does it on a timer)

1. Open **Automatic Correction** and read the header sentence: *Student submits → detected → corrected → report attached → grade 1–9 → returned.*
2. **Background Worker → Turn ON** starts it. It polls on an interval (**POLLING — Every 10 minutes** by default) and only touches assignments that were allowed for correction.
3. **Review mode** decides whether grades are published:
   - **ON** — *"Every grade is saved as a draft; press Approve on a correction to publish the grade and return the work."* This is the safe setting.
   - **OFF** — grades are published and work is returned automatically.
4. Watch the live pipeline on this page. Every node is a real workflow stage, and the stage labels are the same ones the Task Manager uses.

### 3.3 The pipeline stages (what "done" really means)

```
Detected → Correcting → Result validated → Report generated → Report validated →
Report uploaded → Report attached → Attachment verified → Grade drafted →
Grade verified → Returning → Returned → Complete
```

A job reaches **Complete** only when each step has been performed *and verified*. Useful failure names you may see name the step that must be repeated, e.g. `Correction failed`, `Report failed`, `Upload failed`, `Attachment failed`, `Grade failed`, `Grade verification failed`, `Return failed`, plus **Needs attention** and **Blocked by Google Classroom**.

### 3.4 Monitoring in the Task Manager

1. Open **Task Manager**. The tiles show **0 active**, the worker cap, the queue and the agent state.
2. The list holds every record: corrections, Classroom workflows and syncs. Filter by **type** and by **state**.
3. Each row shows the kind (**Correction**, **Classroom**, **Sync**), the student, the status, the attempt (e.g. **1/3**) and the elapsed time.
4. Row actions you may see: **Open result**, **Retry**, **Cancel**, **Approve grade**, **Retry report upload**, **Retry from …**.
5. The possible statuses are **Running**, **Queued**, **Needs you**, **Completed**, **Failed**, **Cancelled**.

**A "Completed" correction does not prove the marks are right, and it does not prove the report or the grade reached Google Classroom.** Completed only means the job's own steps finished. The report and grade states are shown separately (Step 6).

### 3.5 Long answer booklets

Every submission, including a long booklet, uses the same correction pipeline and appears in the same **Task Manager** list. Long documents take longer, so the job stays **Running** for more of the time; its progress and stage are shown on the row. Use **Retry** or **Cancel** exactly as for any other correction.

---

## Step 4 — Understand the results

1. Open **Results** — *"Every completed correction, searchable and filterable."*
2. Filter with **All**, **Corrected**, **Needs review**, **Overridden**, and narrow by class and assignment.
3. The table shows **STUDENT · ASSIGNMENT · CLASS · SCORE · % · STATUS · CLASSROOM · UPDATED**.
4. Press **Open** to see the **Correction detail**: the student's answers, the question-level marks, the reason given, the total, the provisional grade and the report/grade state.
5. Per-question wording you will see: **Question**, **Correct**, **Partially correct**, **Incorrect**, **Marks lost**, **Reason**. When nothing usable was read from a page you will see **No readable answer extracted.** or **Not provided.** — those need a human look, not acceptance.
6. Confidence is shown per result. Anything flagged appears in the **Review Queue** instead of waiting silently: `LOW`, `VALIDATION_FAILED`, `LARGE_SCORE_CHANGE`, `REVIEW_REQUIRED`, `DOUBLE_CHECK_FAILED`.
7. The report itself can be opened from the detail view. Report states you may see include **Report not attached yet**, **Report uploaded to Drive.**, **Report attached and verified**, **Attached — not verified yet**, **Attached — Drive cleanup pending**, **Report attachment failed**, **Attachment not confirmed on the submission**, and **Grade returned to Classroom**.

**Treat every mark as a recommendation to be checked, not a fact.** That is especially true when the answer is ambiguous, handwritten, partly correct, or hard to read.

---

## Step 5 — Manually verify the correction (the part that cannot be skipped)

The AI can misread handwriting, misunderstand mathematical notation, miss a valid alternative method, apply partial credit wrongly, or work from an incomplete mark scheme. Go through the detail view and compare **the student's actual working with the question and the official mark scheme** — do not simply accept the explanation the app gives.

Look hard at:

- Illegible, faint, cropped or incomplete answers, and pages that appear not to have been processed at all.
- Fractions, negative signs, powers, roots, indices, brackets, decimals and other symbols.
- Diagrams, graphs, tables, constructions and handwritten working.
- Answers that are mathematically equivalent to the mark scheme but written differently.
- Questions with more than one valid method.
- Partially correct solutions that may deserve method marks.
- Questions where the mark-scheme entry is missing or looks wrong.
- Unanswered questions and any page that may not have been read.
- Unusually high or low totals, and results that do not fit the student's actual working.

If a mark needs changing:

1. Open the result and use the review controls — **Set your own mark**, with **Why you changed the mark**. Accepting a flagged result is recorded as *"Accepted by teacher"*; changing one is recorded as *"Result accepted with your override"*.
2. Your decision is what stands. The engine's original mark is kept in the history (**Previous**), and the report can be **Report regenerated from the current marks.**
3. **If the mark scheme itself looks wrong, do not enforce it.** Escalate to the responsible teacher and follow their decision.

### Manual verification checklist

Always required:

- [ ] Correct student and assignment confirmed.
- [ ] Correct question paper and official mark scheme confirmed.
- [ ] All pages and questions appear to have been processed.
- [ ] Mathematical notation and handwritten working were interpreted correctly.
- [ ] Uncertain or flagged answers were manually reviewed.
- [ ] Total marks match the question-level marks.
- [ ] Final grade follows the school's grading rules, not just the percentage.
- [ ] The generated PDF report names the correct student and shows the correct results.

Needed in certain situations only:

- [ ] Partial credit and method marks checked (whenever a mark was lost on a method question).
- [ ] Grade return verified in Google Classroom (always, unless the assignment cannot be published — see Step 6).
- [ ] Report attachment verified and no duplicate Math Checker report left behind (whenever a report was uploaded or replaced).
- [ ] Any unresolved issue recorded and escalated to the teacher.

---

## Step 6 — Return the grade and report

There are **four different things**, and they can succeed or fail independently. Do not treat them as one:

| State | What it means | Where to see it |
|---|---|---|
| Correction completed | The marking job finished; a result exists. | **Task Manager** (status), **Results** |
| Report generated / uploaded | The PDF exists, and was uploaded to Google Drive. | **Results** detail: *Report uploaded to Drive.* |
| Report attached | The PDF is on the student's Classroom submission. | **Results** detail: *Report attached and verified* |
| Grade returned | The mark was published back to Classroom. | **Results** detail: *Grade returned to Classroom* |

**Generating a report does not return a grade, and a success message inside Math Checker does not prove Classroom changed.** Verify in Classroom itself whenever you can.

### 6.1 In review mode (the default)

1. Open **Automatic Correction** or **Task Manager** and find the job that is waiting.
2. Press **Approve** (or **Approve grade** on the Task Manager row) once you are satisfied with the marks. Approving publishes the grade and returns the work.
3. If you are not satisfied, fix the marks first (Step 5) and then approve.

### 6.2 If the report is missing

1. In **Results**, open the student's correction and check the report line.
2. If it reads **Report not attached yet**, **Report attachment failed**, **Could not upload the report to Drive**, **Attachment not confirmed on the submission**, or **Attached — not verified yet**, use **Attach report** (or **Retry attachment**). This retries only the report step — the marking is not repeated.
3. **Google will not allow automatic attachment** and **Classroom refused the attachment** are restrictions from Google, not faults in the marking. In that case download the report and attach it by hand, or ask the teacher.
4. A replaced report may briefly read **Attached — Drive cleanup pending**. That is a tidy-up note, not a failed correction.

### 6.3 If the grade did not update

1. Check the **CLASSROOM** column in **Results** and the grade state in the detail view.
2. If it says **Pending** or **Blocked (review)**, the grade is still waiting: approve the job (6.1) or retry the grade step from **Task Manager** (**Retry from …**). The retry resumes at the step that failed, so the marking and the report are not repeated.
3. If the app reports that Google refused the write, the assignment may not be one Math Checker created. See 6.4.
4. When the app cannot write a grade at all, it shows suggested grades for one-click manual entry — use that and enter the grade in Google Classroom yourself.

### 6.4 Two honest limits you must work with

- **Google only allows the integration that created a coursework item to change its grades or return it.** For **External Classroom** assignments, Math Checker can correct and produce reports but **cannot publish**. Enter the grade in Google Classroom manually.
- Writing **draft grades** back to Classroom is a separate setting (**Settings → Connection → Attempt to write draft grades back to Classroom**) and is **off by default**. If it is off, expect to type grades into Classroom yourself.

### 6.5 Never do this

Do not press the correction again just because the grade or report step failed. Re-running the whole assignment risks a second report and wasted AI time. Retry the **failed step only**, from the row that failed.

---

## Step 7 — Handle errors safely

### Troubleshooting quick reference

Work down the "What to check" column before doing anything. If the safe next action does not apply to your installation, escalate instead of improvising.

| Problem | What to check | Safe next action |
|---|---|---|
| **Correction failed** | **Task Manager** row status and its error detail | Read the error, fix the cause (often the mark scheme or the file), then press **Retry** on that row |
| **Answer appears incorrectly marked** | Student working, the question, and the official mark scheme | Review it yourself, set your own mark, and follow the teacher's marking decision |
| **Report is missing** | Report line in the result detail | **Attach report** / **Retry attachment** — this does not re-mark |
| **Grade did not update** | Grade state in the detail view, and the actual value in Classroom | Approve the job if it is held; otherwise retry the grade step. Check whether the assignment is External (cannot publish) |
| **Duplicate reports appear** | Existing Math Checker attachments on the submission | Use the supported replacement path (**Attach report** replaces the previous Math Checker report where Google allows it). Do not delete attachments by hand |
| **AI provider unavailable** | AI page: keys, the provider badge, quota messages | Wait and retry, or tell the teacher. Commonly a rate limit or an exhausted quota — not a marking error |
| **Submission appears missing** | Classroom sync state and the submission's status in the assignment | Sync/Refresh and investigate. Do **not** correct a draft or a missing submission |
| **Job stuck / nothing moving** | **Task Manager** tiles (queue vs workers) and **Automatic Correction** polling state | Check whether the background worker is ON. Restarting the app requeues interrupted work; it does not lose results |
| **File could not be downloaded / unreadable** | The submission's attachment in Classroom; the error text on the job | Ask the student to re-upload, or have the teacher handle the file. Do not correct an empty or unreadable file |
| **Login/session expired** | The sign-in screen after a period of inactivity | Sign in again with the host account the teacher gave you |
| **Network interruption** | The provider badge and Classroom connection | Wait for the connection, then retry the failed step. Queued work resumes by itself |

### What not to do

- **Never delete or edit the database.** Grades, results and history live in it.
- **Never edit stored grades by hand** outside the app's own review controls.
- **Do not remove unrelated Classroom attachments** or student files.
- **Do not repeatedly restart processing or the whole assignment.** Retry the step that failed.
- **Never share API keys, passwords, tokens or student data** outside authorised channels.
- If the app behaves unexpectedly, **stop the affected operation and investigate** — do not act on a result you do not trust.

---

## Step 8 — Finish and report problems

1. **Results** should show a row for every submission you intended to correct.
2. Filter **Review Queue** and clear anything still flagged: for each row, either **Set your own mark** or accept it as correct. *"Every corrected result passed verification"* means the queue is empty.
3. Check **Task Manager** for rows left in **Needs you**, **Failed** or **Cancelled** — those are the ones with unfinished work.
4. For every grade you published, confirm it in Google Classroom: the mark is there, the report is attached once, and the work is returned.
5. Record anything you could not resolve — an unclear answer, a disputed mark, a missing submission, a mark-scheme concern — and pass it to the teacher.

### Before You Finish — checklist

- [ ] Every intended submission has a result (or a recorded reason why not).
- [ ] The **Review Queue** is empty, or each remaining item is documented.
- [ ] No job is left in **Needs you** / **Failed** that I could have retried safely.
- [ ] Totals and grades were verified against the mark scheme.
- [ ] Reports are attached once, with no duplicate Math Checker report.
- [ ] Grades that could be published were published; grades that could not (External assignments) were entered in Classroom by hand.
- [ ] Unresolved issues, manual overrides and unusual cases were recorded and reported to the teacher.

---

## Safety and precautions

- Never assume AI-generated marks are automatically correct.
- Always confirm the correct assignment, class, student, question paper and mark scheme before processing.
- Manually inspect ambiguous answers and questionable marks.
- Do not publish a grade you are unsure about — hold it, fix it, or escalate it.
- Never assume that a report being generated means the grade was returned.
- Verify the grade and the report on the actual Google Classroom submission when possible.
- Never process the same submission repeatedly without checking its current status.
- Do not delete existing reports or student attachments manually unless you know the procedure and have authorisation.
- Never share API keys, passwords, access tokens or private student information.
- Keep student submissions and reports inside authorised channels only.
- Escalate suspected mark-scheme errors, grading disputes and uncertain cases to the responsible teacher.
- Keep a record of corrections, manual changes and unresolved issues.
- Do not assume a missing, draft or previously graded item should be processed anyway.
- If the system behaves unexpectedly, stop the affected operation and investigate instead of trusting an incorrect result.

### Administrator-only (not for assistants without permission)

These change the installation itself rather than one correction:

- **Settings → Access & accounts** — the host sign-in, Public Address, Multi-Account Mode.
- **Settings → Connection** — the Google Classroom client, draft grade write-back.
- **Settings → Background workers / Student file retention / Grade boundaries / Server lifecycle.**
- **Automatic Correction → Enable automation / Turn ON**, and **Review mode**.
- **Storage → Export data / Import data** and database backup.
- **Privacy & Data → Factory Reset** and diagnostics export.
- **Diagnostics**, **Gemini Prompts**, **Accuracy Audit**, **Multi-School**, **Application Updates** are developer surfaces and do not appear for a normal teacher account.

---

## What Math Checker can and cannot guarantee

**It can:** detect submissions and downloaded work, mark answers against a mark scheme using AI, apply a 1–9 grade scale, produce a PDF report per student, keep a record of every result and every change, retry individual failed steps, and publish grades and return work to Google Classroom when the connection and Google's own rules allow it.

**It cannot:** read every handwritten or scanned answer perfectly; guarantee that the mark scheme supplied is correct or complete; guarantee that every mark is right; guarantee that Google will accept a grade write or an attachment; or guarantee that a result which says "completed" has been checked by a human.

**Who is accountable:** the assistant or teacher responsible for the marking. You confirm the paper and the mark scheme, you check the marks that matter, you handle anything ambiguous, and you follow the school's grading rules. Math Checker helps you do that faster — it does not take that responsibility.

---

## Verification note (how this guide was checked)

Every page name, button label, status word and setting in this guide was taken from the running application (version 5.2.1, build 20261012-2) and from the application's own code.

Verified by looking at the running app: the **Assignments** table and its statuses, the assignment detail (including the External Classroom publishing restriction and the Classroom total points warning), **Results** and its filters and columns, the **Review Queue**, **Task Manager** rows and actions, the **Automatic Correction** header, pipeline stages and review-mode wording, the **Correct a PDF** queue controls, **Storage** status, the **Google Classroom** page states, and the sign-in/settings surfaces.

Not verified here, because this environment has no authorised Google Classroom account: a real grade write-back and report attachment seen inside Google Classroom, and the exact appearance of a live automatic run. The labels and states for those steps were read from the application itself, and the Classroom-side verification steps above are written as checks you must perform — treat them as instructions, not as something already proven.
