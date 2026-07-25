#!/usr/bin/env bash
version=.01
# ============================================================================ #
# GENERATED-COPY NOTICE — canonical source: clients/lib/ini-parser.sh          #
# clients/linux/ini-parser.sh and clients/t3/ini-parser.sh are byte-identical  #
# generated copies (the client deploy paths only ship flat per-platform files, #
# so the lib cannot be served directly). Edit clients/lib/ini-parser.sh, then  #
# re-sync:  cp clients/lib/ini-parser.sh clients/linux/ini-parser.sh           #
#           cp clients/lib/ini-parser.sh clients/t3/ini-parser.sh              #
# Verify:   cmp clients/lib/ini-parser.sh clients/linux/ini-parser.sh          #
# Test:     bash clients/tests/ini_parser_test.sh                              #
# ============================================================================ #
# -------------------------------------------------------------------------------- #
# Description                                                                      #
# -------------------------------------------------------------------------------- #
# A 'complete' ini file parsers written in pure bash (4), it was written for no    #
# other reason that one did not exist. It is completely pointless apart from some  #
# clever tricks.                                                                   #
#                                                                                  #
# Fork-free: the parse loop and get_value use only bash builtins / parameter       #
# expansion — no tr/sed/echo subshells. The sim loop calls get_value dozens of     #
# times per cycle on small hardware, so every fork counts.                         #
# -------------------------------------------------------------------------------- #

# -------------------------------------------------------------------------------- #
# Shell options                                                                    #
# -------------------------------------------------------------------------------- #
# extglob is needed by the *( ) / +( ) patterns in the parameter expansions        #
# below. Enable it at SOURCE time — get_value can legitimately be called before    #
# process_ini_file (which used to be the only place it was set).                   #
# -------------------------------------------------------------------------------- #

shopt -s extglob

# -------------------------------------------------------------------------------- #
# Global Variables                                                                 #
# -------------------------------------------------------------------------------- #
# Global variables which can be set by the calling script, but need to be declared #
# here also to ensure the script is clean and error free.                          #
#                                                                                  #
# case_sensitive_sections - should section names be case sensitive                 #
# case_sensitive_keys     - should key names be case sensitive                     #
# default_to_uppercase    - If we are using case insensitive, default to uppercase #
# show_config_warnings    - should we show config warnings                         #
# show_config_errors      - should we show config errors                           #
# -------------------------------------------------------------------------------- #

declare case_sensitive_sections=""
declare case_sensitive_keys=""
declare default_to_uppercase=""
declare show_config_warnings=""
declare show_config_errors=""

# -------------------------------------------------------------------------------- #
# Default Section                                                                  #
# -------------------------------------------------------------------------------- #
# Any values that are found outside of a defined section need to be put somewhere  #
# so they can be recalled as needed. Sections is set up with a 'default' for this  #
# purpose.                                                                         #
# -------------------------------------------------------------------------------- #

DEFAULT_SECTION='default'

sections=()

# -------------------------------------------------------------------------------- #
# Local Variables                                                                  #
# -------------------------------------------------------------------------------- #
# The local variables which can be overridden by the global variables above.       #
#                                                                                  #
# local_case_sensitive_sections - should section names be case sensitive           #
# local_case_sensitive_keys     - should key names be case sensitive               #
# default_to_uppercase          - should we default to uppercase                   #
# local_show_config_warnings    - should we show config warnings                   #
# local_show_config_errors     - should we show config errors                      #
# -------------------------------------------------------------------------------- #

local_case_sensitive_sections=true
local_case_sensitive_keys=true
local_default_to_uppercase=false
local_show_config_warnings=false
local_show_config_errors=true

# -------------------------------------------------------------------------------- #
# Internal fork-free helpers                                                       #
# -------------------------------------------------------------------------------- #
# Each helper leaves its output in _ini_result instead of echoing, so callers      #
# never need a $( ) command substitution (each of which forks a subshell). The     #
# public process_* / handle_default_case wrappers below keep the old echo API.     #
# -------------------------------------------------------------------------------- #

_ini_result=''

# ASCII case conversion without `tr`. Inputs are already cleansed to               #
# [a-zA-Z0-9_] wherever this is used, so ASCII-only handling is equivalent.        #
_ini_tolower()
{
    local str=${1-} out='' ch pre
    local U='ABCDEFGHIJKLMNOPQRSTUVWXYZ' L='abcdefghijklmnopqrstuvwxyz'
    # NOTE: [[:upper:]]/[[:lower:]] classes, never [A-Z]/[a-z] ranges — glob
    # ranges are locale-collation-dependent and can match BOTH cases.
    if [[ ${str} != *[[:upper:]]* ]]; then
        _ini_result=${str}
        return 0
    fi
    local i len=${#str}
    for (( i=0; i<len; i++ )); do
        ch=${str:i:1}
        pre=${U%%"${ch}"*}
        if [[ ${pre} != "${U}" ]]; then
            ch=${L:${#pre}:1}
        fi
        out+=${ch}
    done
    _ini_result=${out}
}

_ini_toupper()
{
    local str=${1-} out='' ch pre
    local U='ABCDEFGHIJKLMNOPQRSTUVWXYZ' L='abcdefghijklmnopqrstuvwxyz'
    if [[ ${str} != *[[:lower:]]* ]]; then
        _ini_result=${str}
        return 0
    fi
    local i len=${#str}
    for (( i=0; i<len; i++ )); do
        ch=${str:i:1}
        pre=${L%%"${ch}"*}
        if [[ ${pre} != "${L}" ]]; then
            ch=${U:${#pre}:1}
        fi
        out+=${ch}
    done
    _ini_result=${out}
}

# Case-fold per local_default_to_uppercase — the fork-free core of                 #
# handle_default_case.                                                             #
_ini_default_case()
{
    if [[ "${local_default_to_uppercase}" = false ]]; then
        _ini_tolower "${1-}"
    else
        _ini_toupper "${1-}"
    fi
}

# Cleanse a section/key name: trim spaces, squash runs of punctuation/blanks to    #
# a single underscore, drop anything not [a-zA-Z0-9_]. Same transform the old      #
# `tr -s '[:punct:] [:blank:]' '_' | sed 's/[^a-zA-Z0-9_]//g'` pipeline did.       #
_ini_cleanse_name()
{
    local str=${1-}
    str="${str##*( )}"                        # Remove leading spaces
    str="${str%%*( )}"                        # Remove trailing spaces
    str="${str//+([[:punct:][:blank:]])/_}"   # Runs of :punct:/:blank: -> one underscore
    str="${str//[!a-zA-Z0-9_]/}"              # Remove non-alphanumerics (except underscore)
    _ini_result=${str}
}

_ini_section_name()
{
    _ini_cleanse_name "${1-}"
    if [[ "${local_case_sensitive_sections}" = false ]]; then
        _ini_default_case "${_ini_result}"
    fi
}

_ini_key_name()
{
    _ini_cleanse_name "${1-}"
    if [[ "${local_case_sensitive_keys}" = false ]]; then
        _ini_default_case "${_ini_result}"
    fi
}

# Cleanse a value: strip inline comments, trim, escape single quotes.              #
_ini_value()
{
    local value=${1-}
    value="${value%%\;*}"                     # Remove in line right comments
    value="${value%%\#*}"                     # Remove in line right comments
    value="${value##*( )}"                    # Remove leading spaces
    value="${value%%*( )}"                    # Remove trailing spaces
    value=${value//\'/SINGLE_QUOTE}           # escape_string
    _ini_result=${value}
}

# -------------------------------------------------------------------------------- #
# Set Global Variables                                                             #
# -------------------------------------------------------------------------------- #
# Check to see if the global overrides are set and if so, override the defaults.   #
#                                                                                  #
# Error checking is in place to ensure that the override contains a valid value of #
# true or false, anything else is ignored.
# -------------------------------------------------------------------------------- #

function setup_global_variables
{
    if [[ -n "${case_sensitive_sections}" ]] && [[ "${case_sensitive_sections}" = false || "${case_sensitive_sections}" = true ]]; then
         local_case_sensitive_sections=${case_sensitive_sections}
    fi

    if [[ -n "${case_sensitive_keys}" ]] && [[ "${case_sensitive_keys}" = false || "${case_sensitive_keys}" = true ]]; then
         local_case_sensitive_keys=${case_sensitive_keys}
    fi

    if [[ -n "${default_to_uppercase}" ]] && [[ "${default_to_uppercase}" = false || "${default_to_uppercase}" = true ]]; then
         local_default_to_uppercase=${default_to_uppercase}
    fi

    if [[ -n "${show_config_warnings}" ]] && [[ "${show_config_warnings}" = false || "${show_config_warnings}" = true ]]; then
         local_show_config_warnings=${show_config_warnings}
    fi

    if [[ -n "${show_config_errors}" ]] && [[ "${show_config_errors}" = false || "${show_config_errors}" = true ]]; then
         local_show_config_errors=${show_config_errors}
    fi

    _ini_default_case "${DEFAULT_SECTION}"
    DEFAULT_SECTION=${_ini_result}

    # Move to from global settting to handle default uppercase option
    sections+=("${DEFAULT_SECTION}")
}

# -------------------------------------------------------------------------------- #
# in Array                                                                         #
# -------------------------------------------------------------------------------- #
# A function to check to see if a given value exists in a given array.             #
# -------------------------------------------------------------------------------- #

function in_array()
{
    local haystack="${1}[@]"
    local needle=${2}

    for i in ${!haystack:-}; do
        if [[ ${i} == "${needle}" ]]; then
            return 0
        fi
    done
    return 1
}

# -------------------------------------------------------------------------------- #
# Show Warning                                                                     #
# -------------------------------------------------------------------------------- #
# A wrapper to display any configuration warnings, taking into account if the      #
# local_show_config_warnings flag is set to true.                                  #
# -------------------------------------------------------------------------------- #

function show_warning()
{
    if [[ "${local_show_config_warnings}" = true ]]; then
        format=$1
        shift;

        # shellcheck disable=SC2059
        printf "[ WARNING ] ${format}" "$@";
    fi
}

# -------------------------------------------------------------------------------- #
# Show Error                                                                       #
# -------------------------------------------------------------------------------- #
# A wrapper to display any configuration errors, taking into account if the        #
# local_show_config_errorss flag is set to true.                                   #
# -------------------------------------------------------------------------------- #

function show_error()
{
    if [[ "${local_show_config_errors}" = true ]]; then
        format=$1
        shift;

        # shellcheck disable=SC2059
        printf "[ ERROR ] ${format}" "$@" >&2;
    fi
}

# -------------------------------------------------------------------------------- #
# Handle Default Case                                                              #
# -------------------------------------------------------------------------------- #
# Handle the default case of a section or key.                                     #
# -------------------------------------------------------------------------------- #

function handle_default_case()
{
    _ini_default_case "${1-}"
    echo "${_ini_result}"
}


# -------------------------------------------------------------------------------- #
# Process Section Name                                                             #
# -------------------------------------------------------------------------------- #
# Once we have located a section name within the given config file, we need to     #
# 'cleanse' the value.                                                             #
# -------------------------------------------------------------------------------- #

function process_section_name()
{
    _ini_section_name "${1-}"
    echo "${_ini_result}"
}

# -------------------------------------------------------------------------------- #
# Process Key Name                                                                 #
# -------------------------------------------------------------------------------- #
# Once we have located a key name on a given line, we need to 'cleanse' the value. #
# -------------------------------------------------------------------------------- #

function process_key_name()
{
    _ini_key_name "${1-}"
    echo "${_ini_result}"
}

# -------------------------------------------------------------------------------- #
# Process Value                                                                    #
# -------------------------------------------------------------------------------- #
# Once we have located a value attached to a key, we need to 'cleanse' the value.  #
# -------------------------------------------------------------------------------- #

function process_value()
{
    _ini_value "${1-}"
    echo "${_ini_result}"
}

# -------------------------------------------------------------------------------- #
# Escape string                                                                    #
# -------------------------------------------------------------------------------- #
# Replace ' with SINGLE_QUOTE to avoid issues with eval.                           #
# -------------------------------------------------------------------------------- #

function escape_string()
{
    local clean

    clean=${1//\'/SINGLE_QUOTE}
    echo "${clean}"
}

# -------------------------------------------------------------------------------- #
# Un-Escape string                                                                 #
# -------------------------------------------------------------------------------- #
# Convert SINGLE_QUOTE back to ' when returning the value to the caller.           #
# -------------------------------------------------------------------------------- #

function unescape_string()
{
    local orig

    orig=${1//SINGLE_QUOTE/\'}
    echo "${orig}"
}

# -------------------------------------------------------------------------------- #
# Parse ini file                                                                   #
# -------------------------------------------------------------------------------- #
# Read a named file line by line and process as required.                          #
# -------------------------------------------------------------------------------- #

function process_ini_file()
{
    # Reset all section data from any previous call to prevent value accumulation
    for _reset_s in "${sections[@]:-}"; do
        [[ -n "$_reset_s" ]] || continue
        # Unset every ${section}_${key} scalar from the previous parse too, so
        # get_value's scalar lookup can never serve a stale value for a key
        # that no longer exists in the re-parsed file.
        _reset_keys=()
        eval "_reset_keys=( \"\${${_reset_s}_keys[@]:-}\" )"
        for _reset_k in "${_reset_keys[@]:-}"; do
            [[ -n "$_reset_k" ]] && eval "unset ${_reset_s}_${_reset_k}"
        done
        eval "unset ${_reset_s}_keys ${_reset_s}_values"
    done
    unset _reset_keys
    sections=()
    local line_number=0
    local section="${DEFAULT_SECTION}"
    local key_array_name=''

    setup_global_variables

    # If the config file is missing/unreadable, return without populating so the
    # redirect below does not error out (fatal under a 'set -e'/'set -u' caller).
    # Warn loudly first: an unreadable file (e.g. simulation.conf written 0600 by
    # a root updater) is otherwise indistinguishable from "all flags off" and
    # silently disables every simulation. Don't let it hide again.
    if [[ ! -r "$1" ]]; then
        if [[ -e "$1" ]]; then
            echo "WARN: ini-parser: '$1' exists but is not readable by $(id -un) — all values will be empty" >&2
        fi
        return
    fi

    shopt -s extglob

    while IFS= read -r line || [ -n "${line:-}" ]; do
        line="${line%$'\r'}"  # Remove trailing carriage return if present (CRLF)
        line_number=$((line_number+1))

        if [[ ${line} =~ ^# || ${line} =~ ^\; || -z ${line} ]]; then  # Ignore comments / empty lines
            continue;
        fi

        if [[ ${line} =~ ^"["(.+)"]"$ ]]; then  # Match pattern for a 'section'
            _ini_section_name "${BASH_REMATCH[1]}"
            section=${_ini_result}

            if ! in_array sections "${section}"; then
                eval "${section}_keys=()"  # Use eval to declare the keys array
                eval "${section}_values=()"  # Use eval to declare the values array
                sections+=("${section}")  # Add the section name to the list
            fi
        elif [[ ${line} =~ ^(.*)"="(.*) ]]; then  # Match pattern for a key=value pair
            _ini_key_name "${BASH_REMATCH[1]}"
            key=${_ini_result}
            _ini_value "${BASH_REMATCH[2]}"
            value=${_ini_result}

            if [[ -z ${key} ]]; then
                show_error 'line %d: No key name\n' "${line_number}"
            else
                if [[ "${section}" == "${DEFAULT_SECTION}" ]]; then
                    show_warning '%s=%s - Defined on line %s before first section - added to "%s" group\n' "${key}" "${value}" "${line_number}" "${DEFAULT_SECTION}"
                fi

                eval key_array_name="${section}_keys"

                if in_array "${key_array_name}" "${key}"; then
                    show_warning 'key %s - Defined multiple times within section %s\n' "${key}" "${section}"
                else
                    # First occurrence wins: the ${section}_${key} scalar is what
                    # get_value reads, and the old array scan returned the FIRST
                    # match — so only the first occurrence may set the scalar.
                    eval "${section}_${key}='${value}'"  # Use eval to declare a variable
                fi
                eval "${section}_keys+=(${key})"  # Use eval to add to the keys array
                eval "${section}_values+=('${value}')"  # Use eval to add to the values array
            fi
        fi
    done < "$1"
}

# -------------------------------------------------------------------------------- #
# Get Value                                                                        #
# -------------------------------------------------------------------------------- #
# Retrieve a value for a specific key from a named section.                        #
#                                                                                  #
# Pure-bash indirect lookup on the ${section}_${key} scalar process_ini_file       #
# sets (first occurrence only — preserves the old array scan's first-match-only    #
# semantics, which prevents "offoff" doubling on re-parse). Zero subshells.        #
# -------------------------------------------------------------------------------- #

function get_value()
{
    local section=''
    local key=''
    local var=''
    local value=''

    _ini_section_name "${1-}"
    _ini_default_case "${_ini_result}"
    section=${_ini_result}

    _ini_key_name "${2-}"
    _ini_default_case "${_ini_result}"
    key=${_ini_result}

    var="${section}_${key}"
    # nounset-safe + identifier guard (replaces the old `declare -p
    # ${section}_keys` section check): an unknown or invalid name returns
    # empty output instead of aborting a 'set -u' caller.
    [[ ${var} =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || return 0
    value=${!var-}
    value=${value//SINGLE_QUOTE/\'}   # unescape_string, inline
    printf '%s' "${value}"
}

# -------------------------------------------------------------------------------- #
# Display Config                                                                   #
# -------------------------------------------------------------------------------- #
# Display all of the post processed configuration.                                 #
#                                                                                  #
# NOTE: This is without comments etec.                                             #
# -------------------------------------------------------------------------------- #

function display_config()
{
    local section=''
    local key=''
    local value=''

    for s in "${!sections[@]}"; do
        section=${sections[${s}]}

        printf '[%s]\n' "${section}"

        eval "keys=( \"\${${section}_keys[@]}\" )"
        eval "values=( \"\${${section}_values[@]}\" )"

        for i in "${!keys[@]}"; do
            orig=$(unescape_string "${values[${i}]}")
            printf '%s=%s\n' "${keys[${i}]}" "${orig}"
        done
    printf '\n'
    done
}

# -------------------------------------------------------------------------------- #
# Display Config by Section                                                        #
# -------------------------------------------------------------------------------- #
# Display all of the post processed configuration for a given section.             #
#                                                                                  #
# NOTE: This is without comments etec.                                             #
# -------------------------------------------------------------------------------- #

function display_config_by_section()
{
    local section=$1
    local key=''
    local value=''
    local keys=''
    local values=''

    section=$(handle_default_case "${section}")
    printf '[%s]\n' "${section}"

    eval "keys=( \"\${${section}_keys[@]}\" )"
    eval "values=( \"\${${section}_values[@]}\" )"

    for i in "${!keys[@]}"; do
        orig=$(unescape_string "${values[${i}]}")
        printf '%s=%s\n' "${keys[${i}]}" "${orig}"
    done
    printf '\n'
}

# -------------------------------------------------------------------------------- #
# End of Script                                                                    #
# -------------------------------------------------------------------------------- #
# This is the end - nothing more to see here.                                      #
# -------------------------------------------------------------------------------- #
