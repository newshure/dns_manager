# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""named.conf 파싱.

직접 파서를 만들지 않는다 — `named-checkconf -p` 가 include 를 모두 전개하고
정규화된 설정을 출력하므로, 그 출력만 토큰 단위로 읽는다. include 추적·매크로·
주석 처리를 BIND 에 위임하는 것이 정확하고 유지보수도 싸다.

여기서 다루는 것은 "구조"일 뿐이며, 설정의 유효성 판정은 named-checkconf 자신이 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Statement:
    """`tokens { children } ;` 형태의 설정 문 하나."""

    tokens: list[str]
    children: list["Statement"] = field(default_factory=list)
    has_block: bool = False

    @property
    def keyword(self) -> str:
        return self.tokens[0] if self.tokens else ""

    def find(self, keyword: str) -> "Statement | None":
        for child in self.children:
            if child.keyword == keyword:
                return child
        return None

    def value(self, keyword: str) -> str | None:
        """`keyword value;` 형태의 단일 값을 돌려준다."""
        stmt = self.find(keyword)
        if stmt is None or len(stmt.tokens) < 2:
            return None
        return stmt.tokens[1]

    def values(self, keyword: str) -> list[str]:
        """`keyword { a; b; };` 형태의 목록을 평탄하게 돌려준다.

        블록이 있으면 **블록 안의 값만** 센다. `listen-on port 53 { 127.0.0.1; }` 처럼
        키워드와 블록 사이에 수식어가 오는 문이 있어, 그것까지 주소로 섞으면
        "외부에서 받고 있나" 같은 판단이 틀어진다.
        """
        stmt = self.find(keyword)
        if stmt is None:
            return []
        if stmt.has_block:
            out: list[str] = []
            for child in stmt.children:
                out.extend(child.tokens)
            return out
        return list(stmt.tokens[1:])


def tokenize(text: str) -> list[str]:
    """설정 텍스트를 토큰 목록으로 분해한다. `{` `}` `;` 는 각각 하나의 토큰."""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif ch == "#":
            i = text.find("\n", i)
            if i == -1:
                break
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch == '"':
            end = text.find('"', i + 1)
            if end == -1:
                tokens.append(text[i + 1 :])
                break
            tokens.append(text[i + 1 : end])
            i = end + 1
        elif ch in "{};":
            tokens.append(ch)
            i += 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n{};\"#":
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def parse(text: str) -> list[Statement]:
    """설정 텍스트를 Statement 트리로 파싱한다."""
    tokens = tokenize(text)
    pos = 0

    def parse_block() -> list[Statement]:
        nonlocal pos
        statements: list[Statement] = []
        current: list[str] = []
        children: list[Statement] = []
        has_block = False
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "{":
                pos += 1
                children = parse_block()
                has_block = True
            elif tok == "}":
                pos += 1
                if current or has_block:
                    statements.append(Statement(current, children, has_block))
                return statements
            elif tok == ";":
                pos += 1
                if current or has_block:
                    statements.append(Statement(current, children, has_block))
                current, children, has_block = [], [], False
            else:
                current.append(tok)
                pos += 1
        if current or has_block:
            statements.append(Statement(current, children, has_block))
        return statements

    return parse_block()


def iter_zones(statements: list[Statement]) -> list[tuple[str | None, Statement]]:
    """zone 문을 (view 이름, 문) 쌍으로 모은다. view 밖의 zone 은 view 가 None."""
    found: list[tuple[str | None, Statement]] = []

    def walk(stmts: list[Statement], view: str | None) -> None:
        for stmt in stmts:
            if stmt.keyword == "zone" and stmt.has_block:
                found.append((view, stmt))
            elif stmt.keyword == "view" and stmt.has_block:
                walk(stmt.children, stmt.tokens[1] if len(stmt.tokens) > 1 else "")
            elif stmt.has_block:
                walk(stmt.children, view)

    walk(statements, None)
    return found


def find_options(statements: list[Statement]) -> Statement | None:
    for stmt in statements:
        if stmt.keyword == "options" and stmt.has_block:
            return stmt
    return None
