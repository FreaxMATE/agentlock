#!/usr/bin/env bash
# Smoke test: builds a throwaway project and checks the sandbox rules from
# inside. Usage: tests/smoke.sh [path/to/agentlock]   (default: ./result/bin/agentlock)
# Does not start Claude (no API use).
set -u
AL=$(realpath "${1:-result/bin/agentlock}")
T=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/agentlock-smoke.XXXXXX")
trap 'rm -rf "$T"' EXIT
cd "$T" || exit 1
git init -q
git config user.name "Smoke Test"
git config user.email smoke@example.com
printf 'SECRET_KEY="abc 123" # comment\nexport DB_URL=postgres://x\nMULTI="a\nb"\n' >.env
mkdir -p sub build
echo 'X=1' >sub/.env.local
echo 'Y=1' >build/.env
echo 'Z=1' >secret.txt
ln -s secret.txt .env.link
echo 'A=1' >.env.example
echo hi >README.md
printf '#!/bin/sh\necho REPO-HOOK-RAN >&2\n' >.git/hooks/pre-commit
chmod +x .git/hooks/pre-commit

"$AL" init 2>/dev/null

pass=0
fail=0
check() { # name, expected (0 = command succeeds), command
  local name=$1 want=$2
  shift 2
  out=$("$AL" shell -c "$*" 2>&1 </dev/null)
  got=$?
  if { [ "$want" = 0 ] && [ $got = 0 ]; } || { [ "$want" != 0 ] && [ $got != 0 ]; }; then
    pass=$((pass + 1))
    echo "ok    $name"
  else
    fail=$((fail + 1))
    echo "FAIL  $name (exit $got)"
    echo "$out" | grep -v '^\[INFO\]\|Multiple default' | sed 's/^/      /' | tail -5
  fi
}

check "env injected"                0 '[ "$SECRET_KEY" = "abc 123" ] && [ "$DB_URL" = postgres://x ] && [ "$MULTI" = "a
b" ]'
check ".env looks empty"            0 '[ ! -s .env ]'
check "nested .env hidden"          0 '[ ! -s sub/.env.local ] && [ ! -s build/.env ]'
check "symlinked .env target hidden" 0 '[ ! -s .env.link ] && [ ! -s secret.txt ]'
check ".env.example visible"        0 'grep -q A=1 .env.example'
check ".env not writable"           1 'echo x > .env'
check ".agentlock.toml read-only"   1 'echo x >> .agentlock.toml'
check ".vscode locked"              1 'echo {} > .vscode/settings.json'
check ".vscode cannot be renamed"   1 'mv .vscode .vscode2'
check "~/.claude/settings.json ro"  1 'echo x >> ~/.claude/settings.json'
check "~/.claude/hooks ro"          1 'touch ~/.claude/hooks/x'
check "~/.claude memory writable"   0 'mkdir -p ~/.claude/projects/agentlock-smoke && rmdir ~/.claude/projects/agentlock-smoke'
check "home is empty"               0 '[ ! -e ~/.ssh ] && [ ! -e ~/.config/git ]'
check "internet works"              0 'curl -sSf -o /dev/null https://example.com'
check "localhost blocked"           1 'curl -sS -m 3 -o /dev/null http://127.0.0.1:22'
check "commit works, unsigned"      0 'git add README.md && git commit -qm "add readme" && [ "$(git log -1 --format=%G?)" = N ]'
check "repo hook still runs"        0 'echo a >> README.md; git commit -qam "two" 2>&1 | grep -q REPO-HOOK-RAN'
check "attribution rejected"        1 'echo b >> README.md; git commit -qam "x

Co-Authored-By: Claude <noreply@anthropic.com>"'
check "em dash rejected"            1 "echo c >> README.md; git commit -qam \"\$(printf 'a \\342\\200\\224 b')\""
check "comment-looking line checked" 1 "echo d >> README.md; git commit -qm \"\$(printf 'x\\n# Co-Authored-By: Claude')\" -a"
check "bullet message allowed"      0 'echo e >> README.md; git commit -qam "- add new thing"'
check "--no-verify blocked"         1 'echo f >> README.md; git commit -q --no-verify -am y'
check "--no-ver blocked"            1 'git commit -q --no-ver -am y'
check "-anm blocked"                1 'git commit -qanm y'
check "hooksPath change blocked"    1 'git -c core.hooksPath=/dev/null commit -qam y'
check "alias trick blocked"         1 'git -c alias.p="push --no-verify" p'
check "push blocked"                1 'git push https://github.com/example/example HEAD'
check "push via real git blocked"   0 'g=$(ls -d /nix/store/*-git-[0-9]*/bin/git | head -1); $g push https://github.com/example/example HEAD 2>&1 | grep -q agentlock-push-blocked'

# A config planted in a subfolder must not count.
printf '[git]\nallow_push = true\n' >sub/.agentlock.toml
out=$(cd sub && "$AL" status 2>&1)
if echo "$out" | grep -q "push blocked"; then
  pass=$((pass + 1)); echo "ok    planted subfolder config ignored"
else
  fail=$((fail + 1)); echo "FAIL  planted subfolder config ignored"
fi

# A config widened outside agentlock is not trusted.
sed -i 's|^# read_write = .*|read_write = ["/tmp"]|' .agentlock.toml
if "$AL" status 2>&1 | grep -q "extra rw:  -"; then
  pass=$((pass + 1)); echo "ok    untrusted widening ignored"
else
  fail=$((fail + 1)); echo "FAIL  untrusted widening ignored"
fi
"$AL" trust >/dev/null 2>&1
if "$AL" status 2>&1 | grep -q "extra rw:  /tmp"; then
  pass=$((pass + 1)); echo "ok    trusted after agentlock trust"
else
  fail=$((fail + 1)); echo "FAIL  trusted after agentlock trust"
fi

echo
echo "$pass passed, $fail failed"
[ $fail = 0 ]
