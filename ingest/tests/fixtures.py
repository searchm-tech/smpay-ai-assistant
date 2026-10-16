"""테스트용 샘플. Notion 마크다운 형식은 공식 문서(enhanced markdown v1) 기반의 추정이며,
실제 응답으로 대조해야 한다(docs/design.md 의 R11). 형식이 다르면 여기와 cleaner 를 함께 고친다."""

TOKEN = "ntn_TESTTOKEN_" + "x" * 30
SENTINEL = "ZZ-민감-문서-본문-ZZ"

SIGNED = (
    "https://prod-files-secure.s3.us-west-2.amazonaws.com/abc/def/file.pdf"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAXXXX%2F20261016"
    "&X-Amz-Date=20261016T000000Z&X-Amz-Expires=3600&X-Amz-Signature=deadbeef1234567890"
)
SIGNED_OTHER = SIGNED.replace("deadbeef1234567890", "cafebabe0987654321")


def pid(n: int) -> str:
    """테스트용 페이지 ID (하이픈 있는 소문자 UUID)."""
    h = f"{n:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def page_url(n: int, title: str = "Page") -> str:
    return f"https://www.notion.so/{title}-{n:032x}"


def sample_markdown(signed: str = SIGNED) -> str:
    return f"""# 충전 한도 안내

광고비 충전 한도는 관리자 승인 후 변경됩니다.

## 신청 절차

1. 신청서를 작성합니다.
2. 승인을 기다립니다.

<callout icon="💡" color="gray_bg">
\t승인은 영업일 기준 1일 이내 처리됩니다.
</callout>

<details>
<summary>자주 묻는 질문</summary>
\t한도는 얼마까지 늘릴 수 있나요? 최대 1억 원입니다.
</details>

<table header-row="true">
<tr><td>구분</td><td>한도</td></tr>
<tr><td>일반</td><td>1,000만 원</td></tr>
</table>

![충전 화면]({signed})
<file src="{signed}">정책 문서.pdf</file>
[자세한 내용](https://example.com/policy)

<page url="{page_url(0x0123456789abcdef0123456789abcdef, "Child")}">하위 문서</page>
<database url="https://www.notion.so/db-{'a' * 32}">문의 목록</database>
<unknown url="https://www.notion.so/x#{'b' * 32}" alt="bookmark"/>
<table_of_contents/>
"""
