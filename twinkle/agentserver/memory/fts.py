"""FTS5 分词与 query 构造——抄 jiuwenswarm internal.py 的正确做法。

jiuwenswarm 用 jieba 词级分词 + 停用词过滤 + 每 token 包引号 + OR 连接,让自然
语言 query 对措辞不同的记忆仍能命中。Twinkle 原先整句包双引号喂 FTS5 = phrase
(所有 token 按序连续)→ 换措辞 query 0 命中,FTS 腿实际废掉。本模块修这个 bug,
并把 jieba 做成可选依赖(对齐 sqlite-vec 软导入模式 store.py:99-108):有 jieba
走词级分词(召回质量高),无 jieba 降级 _space_cjk 逐字空格 + OR(仍能召回,只是
单字语义弱)。停用词表 stopwords_zh.txt 抄自 jiuwenswarm
(jiuwenclaw/resources/stopwords_zh.txt,793 词)。
"""
from __future__ import annotations

import os
import re
from typing import Optional, Set

# CJK 表意文字——逐字加空格让 FTS5 unicode61(不切 CJK)能匹配 CJK 子串。
# jiuwenswarm 靠向量腿做 CJK 召回;Twinkle 的 FTS-only 降级路径(无 API key)
# 也需要 CJK 召回,所以无 jieba 时用逐字空格兜底。有 jieba 时走词级分词。
_CJK_PAT = re.compile("[" + chr(0x3400) + "-" + chr(0x9FFF) + chr(0xF900) + "-" + chr(0xFAFF) + "]")

_STOPWORDS: Optional[Set[str]] = None


def _space_cjk(text: str) -> str:
    """逐字加空格让 unicode61 把每个 CJK 字切成单字 token。拉丁/空白/标点
    不动(unicode61 本就切)。jieba 不可用时的降级分词。"""
    return _CJK_PAT.sub(lambda m: " " + m.group(0) + " ", text)


def _load_stopwords() -> Set[str]:
    """从词表文件加载停用词(单例)。FileNotFoundError 静默降级空集——没词表
    也不阻塞,只是不滤停用词。抄 jiuwenswarm internal.py:_load_stopwords。"""
    global _STOPWORDS
    if _STOPWORDS is not None:
        return _STOPWORDS
    # fts.py 在 twinkle/agentserver/memory/ → 上两级 = twinkle/ → resources/
    resources_dir = os.path.join(os.path.dirname(__file__), "..", "..", "resources")
    filepath = os.path.join(resources_dir, "stopwords_zh.txt")
    words: Set[str] = set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                word = line.strip()
                if word:
                    words.add(word)
    except FileNotFoundError:
        pass
    _STOPWORDS = words
    return words


def _is_valid_fts_token(token: str) -> bool:
    """token 是否值得进 FTS 索引/查询。空串或停用词 → False。抄 jiuwenswarm
    internal.py:_is_valid_fts_token。"""
    token = token.strip()
    if not token:
        return False
    if token in _load_stopwords():
        return False
    return True


def tokenize_for_fts(text: str, save: bool) -> str:
    """为 FTS5 索引(save=True)/查询(save=False)分词,空格 join,过停用词。

    有 jieba:索引走 cut_for_search(细粒度提召回),查询走 cut(粗切)——对齐
    jiuwenswarm internal.py:tokenize_for_fts。无 jieba:降级 _space_cjk 逐字
    空格(单字 token,语义弱但零依赖),同样过停用词(滤英文虚词 + 单字中文
    虚词如 的/了/是)。"""
    try:
        import jieba
    except ImportError:
        return " ".join(t for t in _space_cjk(text).split() if _is_valid_fts_token(t))
    if save:
        tokens = jieba.cut_for_search(text.strip())
    else:
        tokens = jieba.cut(text.strip())
    return " ".join(t for t in tokens if _is_valid_fts_token(t))


def build_fts_query(query: str) -> str:
    """把用户 query 构造成 FTS5 MATCH 串:每 token 包双引号(phrase)+ OR 连接,
    前 10 token。OR 语义=任一 token 命中即记分,换措辞 query 也能召回(修 phrase
    bug)。空串返 ''(调用方应跳过 MATCH)。抄 jiuwenswarm internal.py:build_fts_query,
    加双引号转义(t 的 " → "" 防 token 含引号,比 jiuwenswarm 更稳)。"""
    tokenized = tokenize_for_fts(query, False)
    if not tokenized:
        return ""
    tokens = tokenized.split()
    return " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens[:10])
