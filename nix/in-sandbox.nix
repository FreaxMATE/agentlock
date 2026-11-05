# Things that live inside the sandbox: the git wrapper, its hooks and the
# git config Claude's git uses. The per-launch values (your name and email,
# whether push/commit is allowed, forbidden commit message patterns) are
# files under /etc/agentlock/, written by the agentlock CLI at every launch
# and mounted read-only.
{ pkgs }:
let
  realGit = pkgs.git;

  hookNames = [
    "applypatch-msg"
    "pre-applypatch"
    "post-applypatch"
    "pre-commit"
    "pre-merge-commit"
    "prepare-commit-msg"
    "commit-msg"
    "post-commit"
    "pre-rebase"
    "post-checkout"
    "post-merge"
    "pre-push"
    "post-rewrite"
  ];

  # One script for every hook name: applies agentlock's rules, then hands
  # over to the repository's own hook (if it has one), so project hooks keep
  # working inside the sandbox.
  dispatcher = pkgs.writeShellScript "agentlock-hook" ''
    name=$(basename "$0")
    AGENTLOCK_ALLOW_COMMIT=1
    AGENTLOCK_ALLOW_PUSH=0
    [ -r /etc/agentlock/policy ] && . /etc/agentlock/policy

    deny() {
      echo "agentlock: $*" >&2
      exit 1
    }

    case "$name" in
      pre-commit | pre-merge-commit)
        [ "$AGENTLOCK_ALLOW_COMMIT" = 1 ] || deny "commits are turned off in this sandbox (git.allow_commit = false)."
        ;;
      commit-msg)
        if [ -r /etc/agentlock/commit-reject ]; then
          while IFS= read -r pattern; do
            [ -n "$pattern" ] || continue
            ${pkgs.gnugrep}/bin/grep -qiE -e "$pattern" "$1"
            case $? in
              0) deny "commit message rejected, it matches the forbidden pattern '$pattern' (git.reject_in_commit_message). Rewrite the message without it." ;;
              1) ;;
              *) deny "the pattern '$pattern' in git.reject_in_commit_message is not a valid regular expression." ;;
            esac
          done </etc/agentlock/commit-reject
        fi
        ;;
      pre-push)
        [ "$AGENTLOCK_ALLOW_PUSH" = 1 ] || deny "pushing is turned off in this sandbox (git.allow_push = false)."
        ;;
    esac

    # The project's own hooks: its core.hooksPath (husky, lefthook) or .git/hooks.
    repo_hooks=$(GIT_CONFIG_COUNT=0 ${realGit}/bin/git config --local --get core.hooksPath 2>/dev/null)
    if [ -n "$repo_hooks" ]; then
      case "$repo_hooks" in
        /*) ;;
        *) repo_hooks="$(${realGit}/bin/git rev-parse --show-toplevel)/$repo_hooks" ;;
      esac
    else
      repo_hooks="$(${realGit}/bin/git rev-parse --git-common-dir 2>/dev/null)/hooks"
    fi
    if [ -x "$repo_hooks/$name" ]; then
      exec "$repo_hooks/$name" "$@"
    fi
    exit 0
  '';

  hooks = pkgs.runCommand "agentlock-git-hooks" { } ''
    mkdir -p $out
    ${pkgs.lib.concatMapStrings (n: "ln -s ${dispatcher} $out/${n}\n") hookNames}
  '';

  # The "global" git config inside the sandbox. Your real one is not used:
  # it signs with gpg and points at programs that are not in the sandbox.
  gitconfig = pkgs.writeText "agentlock-gitconfig" ''
    [include]
      path = /etc/agentlock/gitconfig
    [commit]
      gpgSign = false
    [tag]
      gpgSign = false
    [filter "lfs"]
      clean = "${pkgs.git-lfs}/bin/git-lfs clean -- %f"
      smudge = "${pkgs.git-lfs}/bin/git-lfs smudge -- %f"
      process = "${pkgs.git-lfs}/bin/git-lfs filter-process"
      required = true
    [credential "https://github.com"]
      helper = "!f() { test \"$1\" = get && test -n \"$GH_TOKEN\" && echo username=x-access-token && echo \"password=$GH_TOKEN\"; }; f"
  '';

  # git, but with agentlock's rules. It re-applies the hooks setting on every
  # call, so changing environment variables does not switch the rules off.
  # (A determined agent could still call the real git binary by its full
  # path: this stops accidents and casual attempts, it is not a wall.)
  git = pkgs.writeShellScriptBin "git" ''
    AGENTLOCK_ALLOW_COMMIT=1
    AGENTLOCK_ALLOW_PUSH=0
    [ -r /etc/agentlock/policy ] && . /etc/agentlock/policy

    deny() {
      echo "agentlock: $*" >&2
      exit 1
    }

    check_config() {
      key=''${1%%=*}
      case "''${key,,}" in
        core.hookspath) deny "changing core.hooksPath is not allowed in this sandbox." ;;
        alias.*) deny "defining git aliases on the command line is not allowed in this sandbox." ;;
        include.* | includeif.*) deny "including other git config is not allowed in this sandbox." ;;
        url.*) deny "rewriting URLs with -c is not allowed in this sandbox." ;;
      esac
    }

    # Find the subcommand behind git's global options.
    sub=""
    args=("$@")
    i=0
    while [ $i -lt ''${#args[@]} ]; do
      a=''${args[$i]}
      case "$a" in
        -c)
          check_config "''${args[$((i + 1))]:-}"
          i=$((i + 2))
          ;;
        -c*)
          check_config "''${a#-c}"
          i=$((i + 1))
          ;;
        --config-env | --config-env=*) deny "--config-env is not allowed in this sandbox." ;;
        -C | --git-dir | --work-tree | --namespace | --super-prefix | --attr-source)
          i=$((i + 2))
          ;;
        -*) i=$((i + 1)) ;;
        *)
          sub=$a
          break
          ;;
      esac
    done
    rest=("''${args[@]:$((i + 1))}")

    for a in "''${rest[@]}"; do
      [ "$a" = "--" ] && break
      # git accepts any unambiguous abbreviation, like --no-ver.
      if [ ''${#a} -ge 6 ] && [[ "--no-verify" == "$a"* ]]; then
        deny "--no-verify is not allowed in this sandbox (it would skip agentlock's checks)."
      fi
    done

    case "$sub" in
      push | send-pack | http-push)
        [ "$AGENTLOCK_ALLOW_PUSH" = 1 ] || deny "pushing is turned off in this sandbox (git.allow_push = false). Ask the user to push."
        ;;
      commit)
        skip=0
        for a in "''${rest[@]}"; do
          [ "$a" = "--" ] && break
          # The value of an option like -m "- new thing" is not an option.
          if [ $skip = 1 ]; then
            skip=0
            continue
          fi
          case "$a" in
            -m | -F | -c | -C | -t | --message | --file | --reuse-message | --reedit-message | --template | --author | --date | --fixup | --squash | --trailer | --cleanup | --pathspec-from-file)
              skip=1
              continue
              ;;
          esac
          # Short option clusters like -anm: look at each letter until one
          # that takes a value (the rest of the word is that value).
          if [[ "$a" =~ ^-[^-] ]]; then
            cluster=''${a#-}
            while [ -n "$cluster" ]; do
              c=''${cluster:0:1}
              cluster=''${cluster:1}
              case "$c" in
                n) deny "-n (no-verify) is not allowed in this sandbox." ;;
                m | F | c | C | t)
                  # -qam "msg": the value is the next argument.
                  [ -z "$cluster" ] && skip=1
                  break
                  ;;
                S | u) break ;; # optional value, only attached
              esac
            done
          fi
        done
        [ "$AGENTLOCK_ALLOW_COMMIT" = 1 ] || deny "commits are turned off in this sandbox (git.allow_commit = false). Ask the user to commit."
        ;;
      merge | rebase | cherry-pick | revert | am | commit-tree)
        [ "$AGENTLOCK_ALLOW_COMMIT" = 1 ] || deny "commits are turned off in this sandbox (git.allow_commit = false)."
        ;;
      config)
        for a in "''${rest[@]}"; do check_config "$a"; done
        ;;
    esac

    unset GIT_CONFIG_PARAMETERS GIT_CONFIG_NOSYSTEM
    export GIT_CONFIG_GLOBAL=${gitconfig}
    export GIT_CONFIG_SYSTEM=/dev/null
    export GIT_CONFIG_COUNT=2
    export GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=${hooks}
    export GIT_CONFIG_KEY_1=user.useConfigOnly GIT_CONFIG_VALUE_1=true
    exec ${realGit}/bin/git "$@"
  '';
in
{
  inherit git hooks gitconfig;
}
