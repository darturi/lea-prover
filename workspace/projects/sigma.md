# Project sigma

<!-- lea:project id="sigma" -->

## Theorem: odd_plus_one_is_even

<!-- lea:theorem name="odd_plus_one_is_even" proof="workspace/proofs/Lea/Sigma/odd_plus_one_is_even.lean" module="Lea.Sigma.odd_plus_one_is_even" -->

### Signature

```lean
theorem odd_plus_one_is_even {n : Nat} (h : ∃ k : Nat, n = 2 * k + 1) : ∃ j : Nat, n + 1 = 2 * j := by
```

### Description

Prove that for any odd number, if you add one you get an even number

### Solving Process

This proof was completed successfully, then retroactively assigned to this project.

### Lean Location

`workspace/proofs/Lea/Sigma/odd_plus_one_is_even.lean`
