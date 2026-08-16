#!/usr/bin/env bash
# 把仓库里已提交的插件同步到测试环境 ElainaBot_v2/plugins/，并逐文件 SHA1 校验。
#
#   ./sync_to_test.sh AI画图 [其它插件...]
#   ./sync_to_test.sh              # 不带参数时同步仓库根下全部插件目录
#
# 只同步 git 已跟踪的文件；测试环境的 data/ 运行数据原样保留，
# 仓库中已删除的文件会在测试目录一并删除。
# 插件名含中文，所有 git 读取都走 -z 并关掉 quotepath，否则路径会被转义成八进制。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_PLUGINS="${TEST_PLUGINS:-/d/0QQbot/ElainaBot_v2/plugins}"
git_q() { git -C "$REPO" -c core.quotepath=false "$@"; }

[ -d "$TEST_PLUGINS" ] || { echo "测试目录不存在: $TEST_PLUGINS" >&2; exit 1; }

if [ "$#" -gt 0 ]; then
    targets=("$@")
else
    mapfile -t targets < <(git_q ls-files -z | tr '\0' '\n' | cut -d/ -f1 | sort -u | grep -v '\.')
fi

if [ -n "$(git_q status --porcelain -- "${targets[@]}")" ]; then
    echo "⚠ 仓库有未提交改动，请先提交再同步：" >&2
    git_q status --short -- "${targets[@]}" >&2
    exit 1
fi

total=0
for name in "${targets[@]}"; do
    mapfile -d '' -t files < <(git_q ls-files -z -- "$name")
    [ "${#files[@]}" -gt 0 ] || { echo "✗ $name 未被 git 跟踪，跳过" >&2; continue; }

    dest="$TEST_PLUGINS/$name"
    # 先删掉测试目录里仓库已不存在的文件（data/ 与字节码缓存除外）
    if [ -d "$dest" ]; then
        while IFS= read -r stale; do
            rel="${stale#"$dest/"}"
            case "$rel" in data/*|*/__pycache__/*|__pycache__/*) continue ;; esac
            printf '%s\n' "${files[@]}" | grep -qxF "$name/$rel" || { rm -f "$stale"; echo "  - 删除 $rel"; }
        done < <(find "$dest" -type f)
    fi

    for rel in "${files[@]}"; do
        target="$TEST_PLUGINS/$rel"
        mkdir -p "$(dirname "$target")"
        cp -p "$REPO/$rel" "$target"
    done

    ok=0
    for rel in "${files[@]}"; do
        a=$(sha1sum "$REPO/$rel" | cut -d' ' -f1)
        b=$(sha1sum "$TEST_PLUGINS/$rel" | cut -d' ' -f1)
        if [ "$a" = "$b" ]; then
            ok=$((ok + 1))
        else
            echo "  ✗ SHA1 不一致: $rel" >&2
            echo "    仓库 $a / 测试 $b" >&2
            exit 1
        fi
    done
    echo "✓ $name  ${ok}/${#files[@]} 个文件 SHA1 一致"
    total=$((total + ok))
done

echo "同步完成，共校验 $total 个文件 → $TEST_PLUGINS"
