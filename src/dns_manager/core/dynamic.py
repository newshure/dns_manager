# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""RFC 2136 동적 갱신 (nsupdate 경로).

`allow-update` 가 걸린 zone 은 named 가 journal(.jnl)에 변경을 쌓는다. 이런 zone 의 파일을
직접 고치면 journal 과 어긋나 변경이 되살아나거나 사라진다. 그래서 동적 zone 은 파일이 아니라
**프로토콜로** 고친다.

서명은 TSIG 키로 한다. 키는 BIND 가 쓰는 키 파일에서 읽는다(별도 보관본을 만들지 않는다 —
비밀값 사본이 늘어날수록 관리가 나빠진다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import dns.exception
import dns.name
import dns.query
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.tsig
import dns.tsigkeyring
import dns.update

from ..config import Config
from . import fileaudit
from .layout import ZoneEntry

_KEY_BLOCK = re.compile(
    r'key\s+"?(?P<name>[^"\s{]+)"?\s*\{(?P<body>[^}]*)\}', re.IGNORECASE | re.DOTALL
)
_ALGORITHM = re.compile(r"algorithm\s+([^\s;]+)\s*;", re.IGNORECASE)
_SECRET = re.compile(r'secret\s+"([^"]+)"\s*;', re.IGNORECASE)

DEFAULT_SERVER = "127.0.0.1"


class DynamicError(RuntimeError):
    """동적 갱신을 할 수 없거나 서버가 거절했다."""


@dataclass(frozen=True)
class TsigKey:
    name: str
    algorithm: str
    secret: str

    def keyring(self) -> dict:
        return dns.tsigkeyring.from_text({self.name: self.secret})

    @property
    def algorithm_name(self) -> dns.name.Name:
        text = self.algorithm.rstrip(".")
        return dns.name.from_text(text if "-" in text else f"hmac-{text}")


def parse_key_file(text: str, wanted: str | None = None) -> TsigKey | None:
    """BIND 키 파일에서 TSIG 키를 읽는다. 이름을 주면 그 키만 찾는다."""
    for match in _KEY_BLOCK.finditer(text):
        name = match.group("name").rstrip(".")
        if wanted and name.lower() != wanted.rstrip(".").lower():
            continue
        body = match.group("body")
        algorithm = _ALGORITHM.search(body)
        secret = _SECRET.search(body)
        if not algorithm or not secret:
            continue
        return TsigKey(name=name, algorithm=algorithm.group(1).strip('"'), secret=secret.group(1))
    return None


def key_name_for(entry: ZoneEntry) -> str | None:
    """zone 의 allow-update 에 걸린 TSIG 키 이름.

    `allow-update { key "ddns-key"; };` 형태에서 키 이름을 집어낸다.
    """
    tokens = [t.strip('"') for t in entry.allow_update]
    for index, token in enumerate(tokens):
        if token.lower() == "key" and index + 1 < len(tokens):
            return tokens[index + 1]
    # named-checkconf -p 는 `key "name";` 를 한 토큰으로 합쳐 내보내기도 한다
    for token in tokens:
        if token.lower().startswith("key "):
            return token.split(None, 1)[1].strip('"')
    return None


def key_candidates(cfg: Config) -> list[Path]:
    """TSIG 키가 있을 만한 파일들.

    키는 named.conf 가 include 하는 파일에 들어 있는 경우가 대부분이다(설정 분리 관례).
    그래서 include 를 따라간 목록을 포함한다 — 경로를 몇 군데 찍어 두는 것만으로는 놓친다.
    """
    from . import detect

    named_conf = Path(cfg.bind.named_conf)
    candidates: list[Path] = []
    if cfg.bind.rndc_key:
        candidates.append(Path(cfg.bind.rndc_key))
    candidates.append(named_conf)
    candidates.extend(detect.includes_of(named_conf, root=Path(cfg.bind.chroot) if cfg.bind.chroot else None))
    for directory in {named_conf.parent, Path(cfg.bind.zones_conf).parent}:
        candidates.extend(sorted(directory.glob("*.key")))
    return candidates


def load_key(cfg: Config, entry: ZoneEntry) -> TsigKey:
    """zone 에 맞는 TSIG 키를 설정된 키 파일들에서 찾는다."""
    wanted = key_name_for(entry)
    candidates = key_candidates(cfg)

    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            found = parse_key_file(path.read_text(encoding="utf-8", errors="replace"), wanted)
        except OSError:
            continue
        if found is not None:
            # 경로와 키 이름만 남긴다. 비밀값은 어디에도 기록하지 않는다.
            fileaudit.record(path, "read", "key-file", detail=f"TSIG 키 '{found.name}'")
            return found

    raise DynamicError(
        f"'{entry.name}' 의 동적 갱신에 쓸 TSIG 키를 찾지 못했습니다"
        + (f" (키 이름: {wanted})" if wanted else "")
        + ". 키 파일 경로를 Settings 에서 지정하거나, named.conf 의 key 블록을 확인하세요."
    )


def _update(cfg: Config, entry: ZoneEntry) -> tuple[dns.update.Update, TsigKey]:
    key = load_key(cfg, entry)
    update = dns.update.Update(
        dns.name.from_text(entry.name),
        keyring=key.keyring(),
        keyalgorithm=key.algorithm_name,
    )
    return update, key


def _send(cfg: Config, update: dns.update.Update, server: str, timeout: float) -> str:
    try:
        response = dns.query.tcp(update, server, timeout=timeout)
    except dns.exception.DNSException as exc:
        raise DynamicError(f"동적 갱신 요청을 보내지 못했습니다: {exc}") from exc
    code = dns.rcode.to_text(response.rcode())
    if response.rcode() != 0:
        raise DynamicError(f"서버가 동적 갱신을 거절했습니다: {code}")
    return code


def add_record(
    cfg: Config,
    entry: ZoneEntry,
    name: str,
    rtype: str,
    data: str,
    ttl: int = 3600,
    *,
    server: str = DEFAULT_SERVER,
) -> str:
    update, _key = _update(cfg, entry)
    try:
        update.add(name or "@", ttl, rtype.upper(), data)
    except dns.exception.DNSException as exc:
        raise DynamicError(f"레코드 값이 올바르지 않습니다: {exc}") from exc
    return _send(cfg, update, server, cfg.bind.command_timeout)


def delete_record(
    cfg: Config,
    entry: ZoneEntry,
    name: str,
    rtype: str,
    data: str | None = None,
    *,
    server: str = DEFAULT_SERVER,
) -> str:
    update, _key = _update(cfg, entry)
    try:
        if data:
            update.delete(name or "@", rtype.upper(), data)
        else:
            update.delete(name or "@", rtype.upper())
    except dns.exception.DNSException as exc:
        raise DynamicError(f"삭제 대상이 올바르지 않습니다: {exc}") from exc
    return _send(cfg, update, server, cfg.bind.command_timeout)


def replace_record(
    cfg: Config,
    entry: ZoneEntry,
    name: str,
    rtype: str,
    data: str,
    ttl: int = 3600,
    *,
    server: str = DEFAULT_SERVER,
) -> str:
    """같은 이름·타입의 RRset 을 통째로 교체한다 (nsupdate 의 replace)."""
    update, _key = _update(cfg, entry)
    try:
        update.replace(name or "@", ttl, rtype.upper(), data)
    except dns.exception.DNSException as exc:
        raise DynamicError(f"레코드 값이 올바르지 않습니다: {exc}") from exc
    return _send(cfg, update, server, cfg.bind.command_timeout)


def sync_journal(cfg: Config, entry: ZoneEntry) -> None:
    """journal 을 zone 파일에 반영한다 (`rndc sync`).

    동적 zone 을 파일 기준으로 보기 전에 호출하면, 화면과 서버가 어긋나지 않는다.
    Windows DNS Manager 의 'Update Server Data File' 에 대응한다.
    """
    from . import apply as apply_mod

    result = apply_mod._rndc(cfg, "sync", entry.name)  # noqa: SLF001 - 같은 패키지
    if not result.ok:
        raise DynamicError(f"journal 동기화에 실패했습니다: {result.message}")
