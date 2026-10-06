# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""named.conf 계열 설정 파일의 블록 단위 편집.

zoneedit.py 가 zone 파일에 하는 일을 설정 파일에 한다: 재생성하지 않고 해당 블록의
텍스트 범위만 바꾼다. 운영자가 손으로 넣어 둔 주석·정렬·다른 설정을 보존하기 위함이다.

유효성 판정은 named-checkconf 가 한다. 이 모듈은 "어디를 고칠지" 만 안다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ZONE_SUFFIXES = (".in-addr.arpa", ".ip6.arpa")


class ConfEditError(RuntimeError):
    pass


class BlockNotFound(ConfEditError):
    pass


@dataclass(frozen=True)
class Block:
    """`keyword "name" ... { ... };` 형태의 문 하나와 그 텍스트 범위."""

    keyword: str
    name: str | None
    start: int  # 문의 시작(앞선 주석 제외)
    end: int  # 닫는 `;` 다음
    body_start: int  # `{` 다음
    body_end: int  # `}` 위치
    text: str

    @property
    def body(self) -> str:
        return self.text[self.body_start - self.start : self.body_end - self.start]


def _scan(text: str, start: int = 0, end: int | None = None) -> list[Block]:
    """주어진 범위에서 같은 깊이의 문들을 찾는다. 따옴표·주석 안의 괄호는 무시한다."""
    end = len(text) if end is None else end
    blocks: list[Block] = []

    i = start
    stmt_start: int | None = None
    body_start: int | None = None
    depth = 0

    while i < end:
        ch = text[i]

        # 주석
        if text.startswith("//", i) or ch == "#":
            nl = text.find("\n", i)
            i = end if nl == -1 else nl + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = end if close == -1 else close + 2
            continue
        if ch == '"':
            close = text.find('"', i + 1)
            i = end if close == -1 else close + 1
            if stmt_start is None:
                stmt_start = i
            continue

        if ch in " \t\r\n":
            i += 1
            continue

        if stmt_start is None:
            stmt_start = i

        if ch == "{":
            depth += 1
            if depth == 1:
                body_start = i + 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                body_end = i
                # 닫는 중괄호 뒤의 `;` 까지 포함
                j = i + 1
                while j < end and text[j] in " \t\r\n":
                    j += 1
                if j < end and text[j] == ";":
                    j += 1
                header = text[stmt_start:body_start - 1]  # type: ignore[operator]
                tokens = _header_tokens(header)
                blocks.append(
                    Block(
                        keyword=tokens[0] if tokens else "",
                        name=tokens[1] if len(tokens) > 1 else None,
                        start=stmt_start,  # type: ignore[arg-type]
                        end=j,
                        body_start=body_start,  # type: ignore[arg-type]
                        body_end=body_end,
                        text=text[stmt_start:j],  # type: ignore[misc]
                    )
                )
                stmt_start = None
                body_start = None
                i = j
                continue
            i += 1
            continue
        if ch == ";" and depth == 0:
            # 블록 없는 단문 (예: `forward first;`)
            header = text[stmt_start : i + 1]
            tokens = _header_tokens(header)
            blocks.append(
                Block(
                    keyword=tokens[0] if tokens else "",
                    name=tokens[1] if len(tokens) > 1 else None,
                    start=stmt_start,
                    end=i + 1,
                    body_start=i,
                    body_end=i,
                    text=header,
                )
            )
            stmt_start = None
            i += 1
            continue
        i += 1

    return blocks


def _header_tokens(header: str) -> list[str]:
    return [t.strip('"').rstrip(";") for t in re.findall(r'"[^"]*"|[^\s{};]+', header)]


def top_level_blocks(text: str) -> list[Block]:
    return _scan(text)


def find_zone_block(text: str, zone: str) -> Block:
    """zone 블록을 찾는다. 이름 비교는 끝점(.)과 대소문자를 무시한다."""
    wanted = zone.lower().rstrip(".")
    for block in top_level_blocks(text):
        if block.keyword == "zone" and block.name and block.name.lower().rstrip(".") == wanted:
            return block
    raise BlockNotFound(f"zone 블록을 찾을 수 없습니다: {zone}")


def has_zone_block(text: str, zone: str) -> bool:
    try:
        find_zone_block(text, zone)
        return True
    except BlockNotFound:
        return False


def add_zone_block(text: str, block_text: str) -> str:
    """zone 블록을 파일 끝에 덧붙인다."""
    body = text.rstrip("\n")
    separator = "\n\n" if body else ""
    return f"{body}{separator}{block_text.strip()}\n"


def remove_zone_block(text: str, zone: str) -> str:
    """zone 블록과 바로 앞에 붙은 주석 줄들을 함께 지운다."""
    block = find_zone_block(text, zone)
    start = _comment_block_start(text, block.start)
    end = block.end
    # 블록 뒤의 줄바꿈 정리
    while end < len(text) and text[end] in " \t":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return (text[:start] + text[end:]).replace("\n\n\n", "\n\n")


def replace_zone_block(text: str, zone: str, block_text: str) -> str:
    block = find_zone_block(text, zone)
    return text[: block.start] + block_text.strip() + text[block.end :]


def _comment_block_start(text: str, start: int) -> int:
    """문 바로 앞에 붙어 있는 주석 줄들의 시작 위치."""
    line_start = text.rfind("\n", 0, start) + 1
    pos = line_start
    while pos > 0:
        prev_end = pos - 1
        prev_start = text.rfind("\n", 0, prev_end) + 1
        line = text[prev_start:prev_end].strip()
        if line.startswith("//") or line.startswith("#"):
            pos = prev_start
            continue
        break
    return pos


# --------------------------- options 안의 문 편집 ---------------------------


def find_options(text: str) -> Block:
    for block in top_level_blocks(text):
        if block.keyword == "options":
            return block
    raise BlockNotFound("named.conf 에서 options 블록을 찾을 수 없습니다.")


def _inner_blocks(text: str, options: Block) -> list[Block]:
    return _scan(text, options.body_start, options.body_end)


def option_includes(text: str) -> list[str]:
    """options 블록 **안쪽**의 include 경로 목록.

    BIND 는 options 안에서도 include 를 허용한다. 이것을 쓰면 named.conf 자체에 쓰기 권한을
    주지 않고도 전역 전달자 같은 options 설정을 관리할 수 있다(최소 권한 운영).
    """
    try:
        options = find_options(text)
    except BlockNotFound:
        return []
    return [b.name for b in _inner_blocks(text, options) if b.keyword == "include" and b.name]


def get_option(text: str, keyword: str) -> Block | None:
    options = find_options(text)
    for block in _inner_blocks(text, options):
        if block.keyword == keyword:
            return block
    return None


def set_option(text: str, keyword: str, statement: str | None) -> str:
    """options 안의 문 하나를 교체·추가·삭제한다.

    statement 가 None 이면 삭제한다. 들여쓰기는 options 안의 기존 문에서 추론한다.
    """
    options = find_options(text)
    existing = get_option(text, keyword)

    if existing is not None:
        if statement is None:
            return _cut_statement(text, existing)
        return text[: existing.start] + statement.strip() + text[existing.end :]

    if statement is None:
        return text

    # options 블록 첫 줄 바로 뒤에 넣는다.
    indent = _guess_indent(text, options)
    insert_at = options.body_start
    if text[insert_at : insert_at + 1] == "\n":
        insert_at += 1
        return text[:insert_at] + f"{indent}{statement.strip()}\n" + text[insert_at:]
    return text[:insert_at] + f"\n{indent}{statement.strip()}\n" + text[insert_at:]


def _cut_statement(text: str, block: Block) -> str:
    """문 하나를 줄째로 들어낸다(앞쪽 들여쓰기와 뒤쪽 줄바꿈 포함)."""
    start = block.start
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip() == "":
        start = line_start
    end = block.end
    while end < len(text) and text[end] in " \t":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:start] + text[end:]


def _indent_of(text: str, pos: int) -> str | None:
    line_start = text.rfind("\n", 0, pos) + 1
    prefix = text[line_start:pos]
    return prefix if prefix.strip() == "" else None


def _guess_indent(text: str, options: Block) -> str:
    for block in _inner_blocks(text, options):
        indent = _indent_of(text, block.start)
        if indent:
            return indent
    return "\t"


# --------------------------- 블록 생성 ---------------------------


def render_address_list(keyword: str, addresses: list[str], indent: str = "    ") -> str:
    if not addresses:
        return f"{keyword} {{ }};"
    joined = " ".join(f"{a};" for a in addresses)
    return f"{keyword} {{ {joined} }};"


def render_zone_block(
    name: str,
    zone_type: str,
    *,
    file: str | None = None,
    masters: list[str] | None = None,
    forwarders: list[str] | None = None,
    forward_policy: str | None = None,
    allow_update: list[str] | None = None,
    allow_transfer: list[str] | None = None,
    allow_query: list[str] | None = None,
    also_notify: list[str] | None = None,
    comment: str | None = None,
    indent: str = "    ",
) -> str:
    """named.conf 에 넣을 zone 블록 텍스트를 만든다 (마법사 미리보기와 동일한 결과)."""
    lines: list[str] = []
    if comment:
        lines.append(f"// {comment}")
    lines.append(f'zone "{name}" IN {{')
    lines.append(f"{indent}type {zone_type};")
    if file:
        lines.append(f'{indent}file "{file}";')
    if masters:
        lines.append(f"{indent}{render_address_list('masters', masters)}")
    if forwarders:
        lines.append(f"{indent}{render_address_list('forwarders', forwarders)}")
    if forward_policy:
        lines.append(f"{indent}forward {forward_policy};")
    if allow_update is not None:
        lines.append(f"{indent}{render_address_list('allow-update', allow_update)}")
    if allow_transfer is not None:
        lines.append(f"{indent}{render_address_list('allow-transfer', allow_transfer)}")
    if allow_query is not None:
        lines.append(f"{indent}{render_address_list('allow-query', allow_query)}")
    if also_notify:
        lines.append(f"{indent}{render_address_list('also-notify', also_notify)}")
    lines.append("};")
    return "\n".join(lines)
