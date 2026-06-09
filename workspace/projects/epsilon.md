# Project epsilon

<!-- lea:project id="epsilon" -->

## Theorem: even_square_of_even

<!-- lea:theorem name="even_square_of_even" proof="workspace/proofs/Lea/Epsilon/even_square_of_even.lean" module="Lea.Epsilon.even_square_of_even" -->

### Signature

```lean
theorem even_square_of_even {n : Nat} (h : ∃ k : Nat, n = 2 * k) : ∃ j : Nat, n * n = 2 * j := by
```

### Description

Formalize this theorem as part of project epsilon.

Theorem name: even_square_of_even

Natural language statement:
If a natural number n is even, then n squared is even.

Lean signature:
theorem even_square_of_even {n : Nat} (h : ∃ k : Nat, n = 2 * k) : ∃ j : Nat, n * n = 2 * j := by

### Solving Process

To formalize the theorem "even_square_of_even", we need to show that if a natural number \( n \) is even, then \( n^2 \) is also even. We'll start by representing \( n \) as \( 2k \) where \( k \) is a natural number, as given by the hypothesis. The goal is to express \( n^2 \) in the form \( 2j \) where \( j \) is some natural number. This involves some algebraic manipulation. We'll use Lean's `rcases` tactic to unpack the existential condition that provides \( k \), then construct \( j \) such that the required equality holds, concluding with a proof of equality using the `ring` tactic. Let's write this proof now.

I've constructed the proof for the theorem "even_square_of_even". We began by unpacking the hypothesis to express \( n \) as \( 2k \). Using this expression, we showed that \( n^2 = (2k)^2 = 4k^2 = 2(2k^2) \), which is clearly in the form \( 2j \), thus proving that \( n^2 \) is even. To validate the proof, I will run `lean_check` to ensure it compiles without errors.

The proof for the theorem "even_square_of_even" has been successfully verified with no errors or warnings. This theorem is now correctly formalized within the project epsilon.

### Lean Location

`workspace/proofs/Lea/Epsilon/even_square_of_even.lean`

## Theorem: even_square_of_double_plus_double

<!-- lea:theorem name="even_square_of_double_plus_double" proof="workspace/proofs/Lea/Epsilon/even_square_of_double_plus_double.lean" module="Lea.Epsilon.even_square_of_double_plus_double" -->

### Signature

```lean
theorem even_square_of_double_plus_double (a b : Nat) : ∃ j : Nat, (2 * a + 2 * b) * (2 * a + 2 * b) = 2 * j := by
```

### Description

Formalize this theorem as part of project epsilon.

Theorem name: even_square_of_double_plus_double

Natural language statement:
For natural numbers a and b, the square of 2*a + 2*b is even.

Lean signature:
theorem even_square_of_double_plus_double (a b : Nat) :
    ∃ j : Nat, (2 * a + 2 * b) * (2 * a + 2 * b) = 2 * j := by

Use existing project results if helpful, especially the project theorem even_square_of_even.

### Solving Process

2\).
3. Show that the result is of the form \(2 \times j\).

Let's proceed by implementing this plan in Lean:

```lean
import Mathlib
import Lea.Epsilon.even_square_of_even

namespace Lea.Epsilon

theorem even_square_of_double_plus_double (a b : Nat) :
    ∃ j : Nat, (2 * a + 2 * b) * (2 * a + 2 * b) = 2 * j := by
  have h1 : 2 * a + 2 * b = 2 * (a + b) := by
    ring
  rw [h1]
  let n := 2 * (a + b)
  have h2 : ∃ k : Nat, n = 2 * k := ⟨a + b, rfl⟩
  exact even_square_of_even h2

end Lea.Epsilon
```

This proof uses `ring` to establish \((2 * a + 2 * b = 2 * (a + b))\), applies an assignment for `n`, constructs an appropriate `k`, then applies the previously-proven theorem `even_square_of_even`. Now, let's validate the proof by checking for any errors or warnings using `lean_check`.

I have constructed a proof for the theorem `even_square_of_double_plus_double`. The proof uses the fact that \((2 * a + 2 * b)\) can be expressed as \(2 * (a + b)\). We then define \(n := 2 * (a + b)\) and use the previously proven theorem `even_square_of_even` to conclude that the square of this expression, \((2 * (a + b))^2\), is indeed even. Now, I will write this proof to a file and check it for correctness using `lean_check`.

I have constructed a proof for the theorem `even_square_of_double_plus_double`, using the identity that \((2 * a + 2 * b) = 2 * (a + b)\). With this, we defined \(n := 2 * (a + b)\) and then applied the theorem `even_square_of_even` to conclude that the square of this expression is even. Now, I will run `lean_check` to ensure that the file compiles without errors or warnings.

The proof for the theorem `even_square_of_double_plus_double` has been successfully verified with no errors or warnings. This theorem is now correctly formalized within the project epsilon.

### Lean Location

`workspace/proofs/Lea/Epsilon/even_square_of_double_plus_double.lean`
