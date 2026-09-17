"""Проверка адреса кошелька до того, как на него пообещали заплатить.

Крипто-выплата необратима: адрес с одной перепутанной буквой — это деньги,
которые ушли чужому человеку. Площадка при этом отчитается, что выплатила.
Поэтому адрес проверяется заранее и по правилам самой сети, а не «на глаз»:

* **EVM** (Ethereum, Base, Polygon, Arbitrum) — адрес ``0x…``, 20 байт. Часть
  символов в нём — контрольная сумма EIP-55: если буквы в разном регистре,
  регистр обязан сходиться с хешем адреса. Опечатка в одном символе почти
  всегда ломает эту проверку.
* **Solana** — адрес в base58, ровно 32 байта. Такой адрес выдаёт Phantom по
  умолчанию; на него платят площадки, работающие в сети Solana.
* **TRON** — адрес вида ``T…`` с контрольной суммой base58check (выплаты USDT).

Ничего лишнего: ни seed-фразы, ни приватных ключей модуль не читает и не
спрашивает — только публичный адрес получателя.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List, Optional

#: Алфавит base58 (Bitcoin/Solana/TRON): без 0, O, I, l — чтобы не путать.
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: Сколько байт в адресе Solana.
SOLANA_BYTES = 32


# --- keccak-256 (нужен для EIP-55) -------------------------------------------

def _rol(value: int, shift: int) -> int:
    shift %= 64
    return ((value << shift) | (value >> (64 - shift))) & 0xFFFFFFFFFFFFFFFF


_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

#: Смещения rho: строка — координата y, столбец — x.
_KECCAK_RHO = [
    0, 1, 62, 28, 27,
    36, 44, 6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21, 8,
    18, 2, 61, 56, 14,
]


def _keccak_f(state: List[int]) -> None:
    for rnd in range(24):
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
             for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]

        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(state[x + 5 * y], _KECCAK_RHO[x + 5 * y])

        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ (
                    (~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y]
                )
        state[0] ^= _KECCAK_RC[rnd]


def keccak256(data: bytes) -> bytes:
    """Keccak-256 (не SHA3-256: другой суффикс заполнения)."""
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    state = [0] * 25
    for offset in range(0, len(padded), rate):
        chunk = padded[offset:offset + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(chunk[i * 8:(i + 1) * 8], "little")
        _keccak_f(state)

    out = bytearray()
    while len(out) < 32:
        for i in range(rate // 8):
            out += state[i].to_bytes(8, "little")
        if len(out) < 32:
            _keccak_f(state)
    return bytes(out[:32])


# --- base58 ------------------------------------------------------------------

def base58_decode(value: str) -> Optional[bytes]:
    """None, если в строке есть символ не из алфавита base58."""
    number = 0
    for char in value:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            return None
        number = number * 58 + index
    leading = len(value) - len(value.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading + body


def _base58check_ok(value: str) -> bool:
    raw = base58_decode(value)
    if raw is None or len(raw) != 25:
        return False
    payload, checksum = raw[:-4], raw[-4:]
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == checksum


# --- адреса ------------------------------------------------------------------

def evm_checksum(address: str) -> str:
    """Адрес EVM в форме EIP-55 (регистр букв кодирует контрольную сумму)."""
    body = address[2:].lower()
    digest = keccak256(body.encode("ascii")).hex()
    letters = "".join(
        char.upper() if char.isalpha() and int(digest[i], 16) >= 8 else char
        for i, char in enumerate(body)
    )
    return "0x" + letters


def is_evm(address: str) -> bool:
    if len(address) != 42 or not address.startswith("0x"):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in address[2:])


def solana_bytes(address: str) -> Optional[int]:
    raw = base58_decode(address)
    return len(raw) if raw is not None else None


@dataclass
class WalletAddress:
    """Разбор адреса: что это за сеть, годится ли он и что сказать человеку."""

    raw: str
    kind: str = "unknown"          # evm | solana | tron | unknown
    normalized: str = ""           # как адрес стоит хранить
    ok: bool = False
    problem: str = ""
    networks: List[str] = field(default_factory=list)
    advice: str = ""

    @property
    def title(self) -> str:
        return {
            "evm": "EVM-адрес (Ethereum, Base, Polygon, Arbitrum)",
            "solana": "адрес Solana",
            "tron": "адрес TRON (TRC-20)",
        }.get(self.kind, "адрес неизвестного формата")

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "ok": self.ok, "problem": self.problem,
            "networks": list(self.networks), "advice": self.advice,
            "normalized": self.normalized,
        }


def classify(address: str) -> WalletAddress:
    """Проверить адрес получателя и объяснить результат по-русски."""
    raw = (address or "").strip()
    if not raw:
        return WalletAddress(raw=raw, problem="адрес пустой")

    if raw.startswith("0x") or raw.startswith("0X"):
        if not is_evm(raw):
            return WalletAddress(
                raw=raw, problem="адрес EVM состоит из 0x и 40 шестнадцатеричных символов",
                advice="скопируйте адрес кнопкой «Copy» в кошельке, а не руками",
            )
        checksummed = evm_checksum(raw)
        mixed = any(c.islower() for c in raw[2:]) and any(c.isupper() for c in raw[2:])
        if mixed and raw != checksummed:
            return WalletAddress(
                raw=raw, kind="evm", normalized=checksummed, ok=False,
                problem="не сходится контрольная сумма EIP-55: похоже на опечатку в символах",
                advice="сравните адрес с тем, что показывает кошелёк; при копировании опечатки не бывает",
            )
        return WalletAddress(
            raw=raw, kind="evm", normalized=checksummed, ok=True,
            networks=["Ethereum", "Base", "Polygon", "Arbitrum"],
            advice=("годится для выплат в USDC на Base (площадки агентов) и в других "
                    "сетях EVM — на площадке укажите сеть Base, если платят туда"),
        )

    if raw.startswith("T"):
        if not _base58check_ok(raw):
            return WalletAddress(
                raw=raw, kind="tron", problem="адрес TRON не проходит проверку base58check",
                advice="проверьте адрес: в нём нет символов 0, O, I, l — они запрещены в base58",
            )
        return WalletAddress(
            raw=raw, kind="tron", normalized=raw, ok=True, networks=["TRON"],
            advice="годится для выплат USDT в сети TRON (сборы ниже, чем в Ethereum)",
        )

    size = solana_bytes(raw)
    if size == SOLANA_BYTES:
        return WalletAddress(
            raw=raw, kind="solana", normalized=raw, ok=True, networks=["Solana"],
            advice=("годится для площадок, которые платят в USDC на Solana. Помните: у "
                    "адресов Solana нет контрольной суммы, опечатка даёт другой рабочий "
                    "адрес — сверьте первые и последние символы с кошельком. Для выплат "
                    "на Base нужен EVM-адрес того же Phantom (0x…), а не этот"),
        )
    if size is not None:
        return WalletAddress(
            raw=raw, kind="solana", problem=f"адрес Solana должен быть 32 байта, а тут {size}",
            advice="скопируйте адрес в кошельке: усечённый или дописанный символ ломает адрес",
        )
    return WalletAddress(
        raw=raw, problem="формат не распознан: не EVM (0x…), не Solana и не TRON (T…)",
        advice=("скопируйте адрес из кошелька кнопкой «Copy». Phantom даёт адрес Solana, "
                "а для Base нужен EVM-аккаунт (0x…) внутри того же Phantom"),
    )


def mask(address: str) -> str:
    """Показать адрес так, чтобы его было видно и нельзя было перепутать."""
    text = (address or "").strip()
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text
