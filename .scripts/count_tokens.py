#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摄入/操作 token 计数工具（tiktoken 实计）

用法:
  1. 整文件计数: python3 .scripts/count_tokens.py <file1> [file2 ...]
  2. stdin 计数(截取段/命令输出): <command> | python3 .scripts/count_tokens.py -
  3. 混合: python3 .scripts/count_tokens.py file1.md - < file2_section.txt

输出: 每项 token 数 + 总和,供 log.md 记录可信摄入成本。
编码: cl100k_base(GPT-4 系列,中英混合较准)。
"""
import sys
import tiktoken

enc = tiktoken.get_encoding("cl100k_base")

def count(text):
    return len(enc.encode(text))

def main():
    args = sys.argv[1:]
    if not args:
        print("用法: count_tokens.py <file...> | - (stdin)", file=sys.stderr)
        sys.exit(1)
    total = 0
    for a in args:
        if a == "-":
            text = sys.stdin.read()
            n = count(text)
            print(f"  stdin: {n:>7} tokens")
            total += n
        else:
            try:
                with open(a, encoding="utf-8") as f:
                    text = f.read()
                n = count(text)
                print(f"  {a}: {n:>7} tokens")
                total += n
            except Exception as e:
                print(f"  {a}: ERROR {e}", file=sys.stderr)
    print(f"  {'─'*30}")
    print(f"  合计: {total:>7} tokens")

if __name__ == "__main__":
    main()
