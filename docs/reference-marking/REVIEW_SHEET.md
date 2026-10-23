# Marking reference set — human verification sheet

**This sheet is the certification step, and it is not done.** Every
expected mark below is derived from the printed mark scheme clause by
clause by the person who wrote the dataset. A competent maths teacher
must confirm each row against the scheme before any accuracy figure
from this set may be described as certified.

How to sign off: read the scheme clause, read the student answer, read
the ruling, and tick the box only if you agree with the expected mark.
Then record your name in `docs/reference-marking/manifest.json` under
`review.verified_by` (and the date under `review.verified_on`) and
re-run the harness — it will not report CERTIFIED until you do.

## paper-a — Reference Paper A - Algebra, Number and Ratio (O Level style)

- Marks available: **29**
- Expected awarded: **22**

| ✓ | Q | Available | Expected | Categories | Ruling |
|---|---|---|---|---|---|
| [ ] | 1 | 3 | **3** | fully-correct, method-and-accuracy | 3 of 3: 4x = 24 shown (M1), division by 4 performed (M1), x = 6 stated as the answer (A1). |
| [ ] | 2 | 2 | **2** | fully-correct, equivalent-expression | 2 of 2: 36/100 seen (M1) and reduced correctly to 9/25 (A1). An unsimplified 36/100 would earn M1 only. |
| [ ] | 3 | 3 | **3** | nested-subquestion, fully-correct | 3 of 3: (a) B1 for 10, (b) M1 for 3n - 2 = 40, A1 for n = 14. |
| [ ] | 4 | 2 | **0** | missing-answer | 0 of 2: no attempt. Neither clause can be credited, and this must be reported as 0 rather than silently omitted. |
| [ ] | 5 | 2 | **2** | alternative-method, fully-correct | 2 of 2: the grid evidences all four terms (M1) and the simplified answer matches (A1). The method is not the scheme's prose, but the clause says 'or a complete correct grid', so it earns the mark. |
| [ ] | 6 | 3 | **3** | equivalent-expression, fully-correct | 3 of 3: all three clauses evidenced. The final answer is written 3b^2a^3 rather than 3a^3b^2 — the scheme explicitly accepts an equivalent ordering, so A1 stands. |
| [ ] | 7 | 2 | **0** | incorrect | 0 of 2: the signs are wrong. (x - 3)(x - 4) expands to x^2 - 7x + 12, so neither the factorisation clause nor the answer clause is met. A mark would be an over-mark. |
| [ ] | 8 | 3 | **3** | fully-correct, method-and-accuracy | 3 of 3: 3x = 12 (M1), x = 4 (M1), y = 3 (A1). |
| [ ] | 9 | 2 | **1** | partial-credit, method-and-accuracy | 1 of 2: the correct formula with correct substitution earns M1. The arithmetic and the answer are wrong, so A1 (54 cm^2) is not earned. |
| [ ] | 10 | 2 | **2** | follow-through | 2 of 2 by follow-through: this candidate's earlier factorisation was wrong, but the scheme marks this part from THEIR work. x = 3 and x = 4 are exactly the roots of (x - 3)(x - 4), so both ft marks are earned even though the final values differ from the correct pair -4 and -3. |
| [ ] | 11 | 2 | **1** | ambiguous | 1 of 2: 3x = 12 earns M1. The final line offers two values and does not identify one, so the answer is not unambiguous and A1 cannot be awarded. Accepted without comment only when the working names the required value. |
| [ ] | 12 | 3 | **2** | partial-credit, method-and-accuracy | 2 of 3: the division earns M1 and the value 60 earns A1. The unit km/h is never written, so B1 is not earned. |

<details><summary>Scheme clauses and student answers</summary>

**Q1** — Solve 4x + 7 = 31.

Scheme:
- M1: 4x = 24 seen (subtract 7 from both sides)
- M1: divide both sides by 4
- A1: x = 6 (cao)

Student wrote:
```
4x + 7 = 31
4x = 24
x = 6
```
Basis for the expectation: each of the three printed clauses is evidenced in the working

**Q2** — Write 0.36 as a fraction in its simplest form.

Scheme:
- M1: 36/100 seen
- A1: 9/25 (cao)

Student wrote:
```
0.36 = 36/100
= 9/25
```
Basis for the expectation: both printed clauses evidenced; the answer is in the form the scheme requires (lowest terms)

**Q3** — The nth term of a sequence is 3n - 2. (a) Write down the 4th term. (b) Which term of the sequence equals 40?

Scheme:
- B1: 4th term = 10
- M1: sets 3n - 2 = 40
- A1: n = 14 (cao)

Student wrote:
```
(a) 3(4) - 2 = 10
(b) 3n - 2 = 40
3n = 42
n = 14
```
Basis for the expectation: the two sub-parts map onto the three printed clauses

**Q4** — Work out (2^3)^2.

Scheme:
- M1: 2^6 seen (or 8 squared)
- A1: 64 (cao)

Student wrote:
```

```
Basis for the expectation: a clause not evidenced earns nothing; a blank answer still occupies marks in the denominator

**Q5** — Expand (x + 3)(x - 5).

Scheme:
- M1: four correct terms or a complete correct area/grid product (x^2, -5x, 3x, -15)
- A1: x^2 - 2x - 15 (cao)

Student wrote:
```
grid:
       x      +3
  x | x^2    +3x
 -5 | -5x    -15
x^2 - 5x + 3x - 15 = x^2 - 2x - 15
```
Basis for the expectation: the scheme's own 'or' alternative admits the grid method

**Q6** — Simplify 12a^5 b^3 / (4a^2 b).

Scheme:
- M1: coefficient 3
- M1: a^3
- A1: 3a^3 b^2 (or any equivalent ordering such as 3b^2 a^3)

Student wrote:
```
12 / 4 = 3
a^5 / a^2 = a^3
b^3 / b = b^2
= 3b^2a^3
```
Basis for the expectation: A1's own wording admits equivalent orderings; multiplication is commutative

**Q7** — Factorise x^2 + 7x + 12.

Scheme:
- M1: (x + 3)(x + 4) seen
- A1: both factors correct (cao)

Student wrote:
```
(x - 3)(x - 4)
```
Basis for the expectation: the student's product does not expand to the printed expression, so no clause is evidenced

**Q8** — Solve simultaneously: 2x + y = 11 and x - y = 1.

Scheme:
- M1: adds (or otherwise eliminates) to get 3x = 12
- M1: x = 4
- A1: y = 3 (cao, dependent on a correct x)

Student wrote:
```
2x + y = 11
x - y = 1
adding: 3x = 12
x = 4
8 + y = 11 so y = 3
```
Basis for the expectation: all three printed clauses evidenced in order

**Q9** — A triangle has base 12 cm and perpendicular height 9 cm. Work out its area.

Scheme:
- M1: 1/2 x 12 x 9 (correct substitution)
- A1: 54 cm^2 (cao, units required)

Student wrote:
```
= 1/2 x 12 x 9
= 108 cm^2
```
Basis for the expectation: M1 follows the printed substitution clause; A1 requires the cao value 54, which the student did not produce

**Q10** — Hence solve x^2 + 7x + 12 = 0.

Scheme:
- M1 (ft): solves their own factorisation from the previous part
- A1 (ft): the two roots consistent with that factorisation

Student wrote:
```
using my factors: (x - 3)(x - 4) = 0
x = 3 or x = 4
```
Basis for the expectation: the scheme's (ft) clauses are printed against 'their own factorisation', so the correct pair is not required here
Watch out: a marker that applies the correct roots (-4, -3) rather than the candidate's has not done follow-through marking

**Q11** — Solve 5x - 3 = 2x + 9.

Scheme:
- M1: 3x = 12 seen
- A1: x = 4 (cao, a single unambiguous value required)

Student wrote:
```
5x - 3 = 2x + 9
3x = 12
x = 4 or 12
```
Basis for the expectation: A1 is 'cao' - correct answer only - which cannot be met by an answer that leaves the correct value unresolved

**Q12** — A car travels 150 km in 2.5 hours. Work out its average speed.

Scheme:
- M1: 150 / 2.5 seen
- A1: 60 (cao)
- B1: km/h stated

Student wrote:
```
150 / 2.5 = 60
```
Basis for the expectation: B1 is a printed clause about stating the unit; the student did not state it

</details>

## paper-b — Reference Paper B - Nested subquestions, repeated lesson id and a defective scheme header

- Marks available: **7**
- Expected awarded: **6**
- **The scheme header claims 8 marks while the parts sum to 7.** This is deliberate: it is the mark-scheme extraction error the app must flag.

| ✓ | Q | Available | Expected | Categories | Ruling |
|---|---|---|---|---|---|
| [ ] | 1a | 1 | **1** | nested-subquestion, overlapping-lesson-id | 1 of 1: the single printed clause is met. |
| [ ] | 1b | 1 | **1** | nested-subquestion, overlapping-lesson-id | 1 of 1: the single printed clause is met. |
| [ ] | 1c | 2 | **1** | nested-subquestion, partial-credit, overlapping-lesson-id | 1 of 2: 9 + 32 is correct substitution (M1). The total 42 is wrong and 41 is the cao value, so A1 fails. Note the parts are nested inside question 1 and must be marked individually, against 1a/1b/1c. |
| [ ] | 2 | 3 | **3** | nested-subquestion, fully-correct, scheme-total-disagrees | 3 of 3: both method clauses and the answer clause are evidenced. The scheme HEADER states a total that disagrees with the sum of its own per-question marks, so the app should flag the scheme rather than silently trusting either number. |

<details><summary>Scheme clauses and student answers</summary>

**Q1a** — 1 (a) Write down the value of 3^2.

Scheme:
- B1: 9

Student wrote:
```
(a) 9
```
Basis for the expectation: clause evidenced exactly

**Q1b** — 1 (b) Write down the value of 2^5.

Scheme:
- B1: 32

Student wrote:
```
(b) 32
```
Basis for the expectation: clause evidenced exactly

**Q1c** — 1 (c) Work out 3^2 + 2^5.

Scheme:
- M1: 9 + 32 seen
- A1: 41 (cao)

Student wrote:
```
(c) 9 + 32
= 42
```
Basis for the expectation: M1 from the substitution clause; A1 requires the cao value

**Q2** — 2 Solve 7x = 42.

Scheme:
- M1: divide both sides by 7
- M1: 42 / 7
- A1: x = 6

Student wrote:
```
x = 42 / 7
x = 6
```
Basis for the expectation: clauses evidenced; the header/sum disagreement is a scheme-integrity observation, not a marking judgement

</details>

## Sign-off

| Paper | Reviewer | Date | Decision |
|---|---|---|---|
| paper-a |  |  | [ ] agreed / [ ] disputed |
| paper-b |  |  | [ ] agreed / [ ] disputed |
