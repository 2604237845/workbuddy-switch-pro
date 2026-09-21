#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""护栏自检

上游静默同步的安全检查必须是真的有效，不能只是「写了没测」。
这里用真实的上游文件做基准，再人工制造几种"被投毒/被大改"的版本，
确认每一种都会被拦下。

用途：上游更新后跑一遍，确认护栏仍然和脚本结构匹配。
    python selftest.py
"""

import sys

import update as U

BASELINE = U.VENDOR / "workbuddy_daily.py"


def build_cases(good):
    cases = []

    cases.append(("原始文件（预期：放行）", good, True))

    cases.append((
        "新增白名单外的域名（疑似新增外传通道）",
        good.replace(b"https://www.workbuddy.cn", b"https://evil.example.com", 1),
        False,
    ))

    cases.append((
        "新增 shell 调用（上游原本没有）",
        good + b"\nimport os\nos.system('calc')\n",
        False,
    ))

    cases.append((
        "新增 eval 动态执行",
        good + b"\n_D = eval('1+1')\n",
        False,
    ))

    cases.append((
        "关键函数被改名（引擎的排除注入会失准）",
        good.replace(b"def t_sign(", b"def t_sign_renamed("),
        False,
    ))

    cases.append((
        "体积骤减（疑似被截断/替换）",
        good[:2000],
        False,
    ))

    cases.append((
        "语法被破坏",
        good + b"\ndef broken_syntax(:\n",
        False,
    ))

    return cases


def main():
    if not BASELINE.exists():
        print("找不到基准文件: %s" % BASELINE)
        return 1

    cfg = U.load_config()
    guards = cfg["auto_update"]["guards"]
    good = BASELINE.read_bytes()

    print("=" * 70)
    print("护栏自检 · 基准文件 %s（%d 字节）" % (BASELINE.name, len(good)))
    print("=" * 70)

    failed = 0
    for label, data, expect_ok in build_cases(good):
        ok, problems = U.check_new_file(BASELINE.name, data, good, guards)
        hit = (ok == expect_ok)
        if not hit:
            failed += 1
        print("%s %s" % ("✅" if hit else "❌", label))
        print("     结果：%s" % ("放行" if ok else "拦下（%d 条问题）" % len(problems)))
        for p in problems[:3]:
            print("       · %s" % p)

    print("=" * 70)
    if failed:
        print("❌ %d 个用例不符合预期，护栏需要检查" % failed)
        return 1
    print("✅ 全部 %d 个用例符合预期，护栏有效" % len(build_cases(good)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
