"""
JAX implementation for integrals over Gaussian basis functions.

Based upon the closed-form expressions derived in

    Taketa, H., Huzinaga, S., & O-ohata, K. (1966). Gaussian-expansion methods for
    molecular integrals. Journal of the physical society of Japan, 21(11), 2313-2324.
    <https://doi.org/10.1143/JPSJ.21.2313>

Hereafter referred to as the "THO paper"

Related work:

[1] Augspurger JD, Dykstra CE. General quantum mechanical operators. An
    open-ended approach for one-electron integrals with Gaussian bases. Journal of
    computational chemistry. 1990 Jan;11(1):105-11.
    <https://doi.org/10.1002/jcc.540110113>

[2] PyQuante: <https://github.com/rpmuller/pyquante2/>
"""

from dataclasses import asdict
from functools import partial
from itertools import product as cartesian_product
from more_itertools import batched
from typing import Callable

import jax.numpy as jnp
import numpy as np
from jax import jit, tree, vmap
from jax.ops import segment_sum

from mess.basis import Basis, basis_iter
from mess.primitive import Primitive, product
from mess.special import (
    binom,
    binom_factor,
    factorial,
    factorial2,
    gammanu,
    allpairs_indices,
)
from mess.types import Float3, FloatNxN
from mess.units import LMAX

BinaryPrimitiveOp = Callable[[Primitive, Primitive], float]


@partial(jit, static_argnums=(0, 1))
def integrate_dense(basis: Basis, primitive_op: BinaryPrimitiveOp) -> FloatNxN:
    (ii, cl, lhs), (jj, cr, rhs) = basis_iter(basis)
    aij = cl * cr * vmap(primitive_op)(lhs, rhs)
    A = jnp.zeros((basis.num_primitives, basis.num_primitives))
    A = A.at[ii, jj].set(aij)
    A = A + A.T - jnp.diag(jnp.diag(A))
    index = basis.orbital_index.reshape(1, basis.num_primitives)
    out = segment_sum(A, index, num_segments=basis.num_orbitals)
    out = segment_sum(out.T, index, num_segments=basis.num_orbitals)
    return out


@partial(jit, static_argnums=(0, 1))
def integrate_sparse(basis: Basis, primitive_op: BinaryPrimitiveOp) -> FloatNxN:
    offset = [0] + [o.num_primitives for o in basis.orbitals]
    offset = np.cumsum(offset)
    ii, jj = allpairs_indices(basis.num_orbitals)
    indices = []
    batch = []

    for count, idx in enumerate(zip(ii, jj)):
        mesh = [range(offset[i], offset[i + 1]) for i in idx]
        indices += list(cartesian_product(*mesh))
        batch += [count] * (len(indices) - len(batch))

    indices = np.array(indices, dtype=np.int32).T
    batch = np.array(batch, dtype=np.int32)
    cij = jnp.stack([jnp.take(basis.coefficients, idx) for idx in indices]).prod(axis=0)
    pij = [
        tree.map(lambda x: jnp.take(x, idx, axis=0), basis.primitives)
        for idx in indices
    ]
    aij = segment_sum(cij * vmap(primitive_op)(*pij), batch, num_segments=count + 1)

    A = jnp.zeros_like(aij, shape=(basis.num_orbitals, basis.num_orbitals))
    A = A.at[ii, jj].set(aij)
    A = A + A.T - jnp.diag(jnp.diag(A))
    return A


integrate = integrate_dense


def _overlap_primitives(a: Primitive, b: Primitive) -> float:
    @vmap
    def overlap_axis(i: int, j: int, a: float, b: float) -> float:
        idx = [(s, t) for s in range(LMAX + 1) for t in range(2 * s + 1)]
        s, t = jnp.array(idx, dtype=jnp.uint32).T
        out = binom(i, 2 * s - t) * binom(j, t)
        out *= a ** jnp.maximum(i - (2 * s - t), 0) * b ** jnp.maximum(j - t, 0)
        out *= factorial2(2 * s - 1) / (2 * p.alpha) ** s

        mask = (2 * s - i <= t) & (t <= j)
        out = jnp.where(mask, out, 0)
        return jnp.sum(out)

    p = product(a, b)
    pa = p.center - a.center
    pb = p.center - b.center
    out = jnp.power(jnp.pi / p.alpha, 1.5) * p.norm
    out *= jnp.prod(overlap_axis(a.lmn, b.lmn, pa, pb))
    return out


def overlap_basis(basis: Basis) -> FloatNxN:
    return integrate(basis, _overlap_primitives)


def _kinetic_primitives(a: Primitive, b: Primitive) -> float:
    t0 = b.alpha * (2 * jnp.sum(b.lmn) + 3) * _overlap_primitives(a, b)

    def offset_qn(ax: int, offset: int):
        lmn = b.lmn.at[ax].add(offset)
        return Primitive(**{**asdict(b), "lmn": lmn})

    axes = jnp.arange(3)
    b1 = vmap(offset_qn, (0, None))(axes, 2)
    t1 = jnp.sum(vmap(_overlap_primitives, (None, 0))(a, b1))

    b2 = vmap(offset_qn, (0, None))(axes, -2)
    t2 = jnp.sum(b.lmn * (b.lmn - 1) * vmap(_overlap_primitives, (None, 0))(a, b2))
    return t0 - 2.0 * b.alpha**2 * t1 - 0.5 * t2


def kinetic_basis(b: Basis) -> FloatNxN:
    return integrate(b, _kinetic_primitives)


def build_gindex():
    vals = [
        (i, r, u)
        for i in range(LMAX + 1)
        for r in range(i // 2 + 1)
        for u in range((i - 2 * r) // 2 + 1)
    ]
    i, r, u = jnp.array(vals).T
    return i, r, u


gindex = build_gindex()


def _nuclear_primitives(a: Primitive, b: Primitive, c: Float3):
    p = product(a, b)
    pa = p.center - a.center
    pb = p.center - b.center
    pc = p.center - c
    epsilon = 1.0 / (4.0 * p.alpha)

    @vmap
    def g_term(l1, l2, pa, pb, cp):
        i, r, u = gindex
        index = i - 2 * r - u
        g = (
            jnp.power(-1, i + u)
            * jnp.take(binom_factor(l1, l2, pa, pb), i)
            * factorial(i)
            * jnp.power(cp, index - u)
            * jnp.power(epsilon, r + u)
        ) / (factorial(r) * factorial(u) * factorial(index - u))

        g = jnp.where(index <= l1 + l2, g, 0.0)
        return segment_sum(g, index, num_segments=LMAX + 1)

    Gi, Gj, Gk = g_term(a.lmn, b.lmn, pa, pb, pc)
    ids = jnp.arange(3 * LMAX + 1)
    ijk = jnp.arange(LMAX + 1)
    nu = (
        ijk[:, jnp.newaxis, jnp.newaxis]
        + ijk[jnp.newaxis, :, jnp.newaxis]
        + ijk[jnp.newaxis, jnp.newaxis, :]
    )

    W = (
        Gi[:, jnp.newaxis, jnp.newaxis]
        * Gj[jnp.newaxis, :, jnp.newaxis]
        * Gk[jnp.newaxis, jnp.newaxis, :]
        * jnp.take(gammanu(ids, p.alpha * jnp.inner(pc, pc)), nu)
    )

    return -2.0 * jnp.pi / p.alpha * p.norm * jnp.sum(W)


overlap_primitives = jit(_overlap_primitives)
kinetic_primitives = jit(_kinetic_primitives)
nuclear_primitives = jit(_nuclear_primitives)


@partial(jit, static_argnums=0)
def nuclear_basis(basis: Basis):
    def n(atomic_number, position):
        def op(pi, pj):
            return atomic_number * _nuclear_primitives(pi, pj, position)

        return integrate(basis, op)

    return vmap(n)(basis.structure.atomic_number, basis.structure.position)


def build_cindex():
    vals = [
        (i1, i2, r1, r2, u)
        for i1 in range(2 * LMAX + 1)
        for i2 in range(2 * LMAX + 1)
        for r1 in range(i1 // 2 + 1)
        for r2 in range(i2 // 2 + 1)
        for u in range((i1 + i2) // 2 - r1 - r2 + 1)
    ]
    i1, i2, r1, r2, u = jnp.array(vals).T
    return i1, i2, r1, r2, u


cindex = build_cindex()


def _eri_primitives(a: Primitive, b: Primitive, c: Primitive, d: Primitive) -> float:
    p = product(a, b)
    q = product(c, d)
    pa = p.center - a.center
    pb = p.center - b.center
    qc = q.center - c.center
    qd = q.center - d.center
    qp = q.center - p.center
    delta = 1 / (4.0 * p.alpha) + 1 / (4.0 * q.alpha)

    def H(l1, l2, a, b, i, r, gamma):
        # Note this should match THO Eq 3.5 but that seems to incorrectly show a
        # 1/(4 gamma)^(i- 2r) term which is inconsistent with Eq 2.22.
        # Using (4 gamma)^(r - i) matches the reported expressions for H_L
        u = factorial(i) * jnp.take(binom_factor(l1, l2, a, b, 2 * LMAX), i)
        v = factorial(r) * factorial(i - 2 * r) * (4 * gamma) ** (i - r)
        return u / v

    @vmap
    def c_term(la, lb, lc, ld, pa, pb, qc, qd, qp):
        # THO Eq 2.22 and 3.4
        i1, i2, r1, r2, u = cindex
        h = H(la, lb, pa, pb, i1, r1, p.alpha) * H(lc, ld, qc, qd, i2, r2, q.alpha)
        index = i1 + i2 - 2 * (r1 + r2) - u
        x = (-1) ** (i2 + u) * factorial(index + u) * qp ** (index - u)
        y = factorial(u) * factorial(index - u) * delta**index
        c = h * x / y

        mask = (i1 <= (la + lb)) & (i2 <= (lc + ld))
        c = jnp.where(mask, c, 0.0)
        return segment_sum(c, index, num_segments=4 * LMAX + 1)

    Ci, Cj, Ck = c_term(a.lmn, b.lmn, c.lmn, d.lmn, pa, pb, qc, qd, qp)

    ijk = jnp.arange(4 * LMAX + 1)
    nu = (
        ijk[:, jnp.newaxis, jnp.newaxis]
        + ijk[jnp.newaxis, :, jnp.newaxis]
        + ijk[jnp.newaxis, jnp.newaxis, :]
    )
    ids = jnp.arange(12 * LMAX + 1)

    W = (
        Ci[:, jnp.newaxis, jnp.newaxis]
        * Cj[jnp.newaxis, :, jnp.newaxis]
        * Ck[jnp.newaxis, jnp.newaxis, :]
        * jnp.take(gammanu(ids, jnp.inner(qp, qp) / (4.0 * delta)), nu)
    )

    return (
        2.0
        * jnp.pi**2
        / (p.alpha * q.alpha)
        * jnp.sqrt(jnp.pi / (p.alpha + q.alpha))
        * p.norm
        * q.norm
        * jnp.sum(W)
    )


eri_primitives = jit(_eri_primitives)
vmap_eri_primitives = jit(vmap(_eri_primitives))


def gen_ijkl(n: int):
    """Adapted from four-index transformations by S Wilson pg 257"""
    for idx in range(n):
        for jdx in range(idx + 1):
            for kdx in range(idx + 1):
                lmax = jdx if idx == kdx else kdx
                for ldx in range(lmax + 1):
                    yield idx, jdx, kdx, ldx


@partial(jit, static_argnums=0)
def eri_basis_sparse(b: Basis):
    indices = []
    batch = []
    offset = np.cumsum([o.num_primitives for o in b.orbitals])
    offset = np.insert(offset, 0, 0)

    for count, idx in enumerate(gen_ijkl(b.num_orbitals)):
        mesh = [range(offset[i], offset[i + 1]) for i in idx]
        indices += list(cartesian_product(*mesh))
        batch += [count] * (len(indices) - len(batch))

    indices = jnp.array(indices, dtype=jnp.int32).T
    batch = jnp.array(batch, dtype=jnp.int32)
    cijkl = jnp.stack([jnp.take(b.coefficients, idx) for idx in indices]).prod(axis=0)
    pijkl = [
        tree.map(lambda x: jnp.take(x, idx, axis=0), b.primitives) for idx in indices
    ]
    eris = cijkl * vmap_eri_primitives(*pijkl)
    return segment_sum(eris, batch, num_segments=count + 1)


def eri_basis(b: Basis):
    unique_eris = eri_basis_sparse_batched(b, batch_size=1024 * 1024)
    ii, jj, kk, ll = jnp.array(list(gen_ijkl(b.num_orbitals)), dtype=jnp.int32).T

    # Apply 8x permutation symmetry to build dense ERI from sparse ERI.
    eri_dense = jnp.empty_like(unique_eris, shape=(b.num_orbitals,) * 4)
    eri_dense = eri_dense.at[ii, jj, kk, ll].set(unique_eris)
    eri_dense = eri_dense.at[ii, jj, ll, kk].set(unique_eris)
    eri_dense = eri_dense.at[jj, ii, kk, ll].set(unique_eris)
    eri_dense = eri_dense.at[jj, ii, ll, kk].set(unique_eris)
    eri_dense = eri_dense.at[kk, ll, ii, jj].set(unique_eris)
    eri_dense = eri_dense.at[kk, ll, jj, ii].set(unique_eris)
    eri_dense = eri_dense.at[ll, kk, ii, jj].set(unique_eris)
    eri_dense = eri_dense.at[ll, kk, jj, ii].set(unique_eris)
    return eri_dense


def eri_basis_sparse_batched(basis: Basis, batch_size: int):
    batched_eris = []
    offset = np.cumsum([o.num_primitives for o in basis.orbitals])
    offset = np.insert(offset, 0, 0)

    for ijkl_batch in batched(gen_ijkl(basis.num_orbitals), batch_size):
        indices = []
        batch = []
        for count, idx in enumerate(ijkl_batch):
            mesh = [range(offset[i], offset[i + 1]) for i in idx]
            indices += list(cartesian_product(*mesh))
            batch += [count] * (len(indices) - len(batch))

        indices = jnp.array(indices, dtype=jnp.int32).T
        batch = jnp.array(batch, dtype=jnp.int32)
        cijkl = jnp.stack([jnp.take(basis.coefficients, idx) for idx in indices]).prod(
            axis=0
        )
        pijkl = [
            tree.map(lambda x: jnp.take(x, idx, axis=0), basis.primitives)
            for idx in indices
        ]
        eris = cijkl * vmap_eri_primitives(*pijkl)
        batched_eris += [segment_sum(eris, batch, num_segments=count + 1)]

    return jnp.hstack(batched_eris)
