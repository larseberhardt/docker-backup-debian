# Bash completion for docker-backup (plain standard bash, no argcomplete).
#
# Installation: copy to /etc/bash_completion.d/docker-backup (install.sh does this),
# or in the shell:  source completion/docker-backup.bash

_docker_backup() {
    local cur prev cmds cfgdir
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    cmds="create ls restore run doctor check snapshots logs rm remove key set notify templates update"
    cfgdir="/etc/docker-backup/configs"

    _db_names() {
        [ -d "$cfgdir" ] || return 0
        local f
        for f in "$cfgdir"/*.json; do
            [ -e "$f" ] || continue
            local b="${f##*/}"
            printf '%s\n' "${b%.json}"
        done
    }

    # Top-level command
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "$cmds" -- "$cur") )
        return
    fi

    local sub="${COMP_WORDS[1]}"
    case "$sub" in
        run|rm|remove|snapshots|logs|doctor|check|set)
            if [ "$COMP_CWORD" -eq 2 ] && [[ "$cur" != -* ]]; then
                COMPREPLY=( $(compgen -W "$(_db_names)" -- "$cur") )
                return
            fi
            ;;
        key)
            if [ "$COMP_CWORD" -eq 2 ]; then
                COMPREPLY=( $(compgen -W "show" -- "$cur") )
                return
            elif [ "$COMP_CWORD" -eq 3 ]; then
                COMPREPLY=( $(compgen -W "$(_db_names)" -- "$cur") )
                return
            fi
            ;;
        notify)
            if [ "$COMP_CWORD" -eq 2 ]; then
                COMPREPLY=( $(compgen -W "setup test show" -- "$cur") )
                return
            fi
            ;;
        restore)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "--from --from-repo --key-file --name --save-config --snapshot --force --no-custom-restore --restore-cmd --use-template-hooks" -- "$cur") )
            else
                _filedir -d
            fi
            return
            ;;
        create)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-a --all --auto --target --offsite --schedule --name --force --non-interactive --from-template --list-templates --no-db-detect --no-quiesce --exclude --keep-within --pre-cmd --post-cmd --restore-cmd --allow-hooks --dump-user --dump-globals --no-dump-globals" -- "$cur") )
            else
                _filedir -d
            fi
            return
            ;;
        templates)
            if [ "$COMP_CWORD" -eq 2 ]; then
                COMPREPLY=( $(compgen -W "list show" -- "$cur") )
                return
            fi
            ;;
    esac

    # Options per subcommand
    case "$sub" in
        check)  COMPREPLY=( $(compgen -W "--all --read-data-subset --refresh-cache" -- "$cur") ) ;;
        doctor) COMPREPLY=( $(compgen -W "--all" -- "$cur") ) ;;
        run)    COMPREPLY=( $(compgen -W "--all" -- "$cur") ) ;;
        rm|remove) COMPREPLY=( $(compgen -W "--purge-keys --yes" -- "$cur") ) ;;
        logs)   COMPREPLY=( $(compgen -W "-f --follow -n --lines --notify" -- "$cur") ) ;;
        set)    COMPREPLY=( $(compgen -W "--schedule --retention --offsite --offsite-retention --offsite-prune --no-offsite-prune --target --i-know-this-orphans-the-old-repo --exclude --exclude-clear --keep-within --no-keep-within --pre-cmd --post-cmd --restore-cmd --clear-hooks --allow-hooks --no-allow-hooks --quiesce --no-quiesce --dump-user --dump-globals --no-dump-globals --refresh-db-detection" -- "$cur") ) ;;
        update) COMPREPLY=( $(compgen -W "--check --branch -y --yes" -- "$cur") ) ;;
    esac
}
complete -F _docker_backup docker-backup
