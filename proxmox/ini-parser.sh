#!/usr/bin/env bash
# ini-parser.sh — Pure-bash INI file parser (bash 4+)
# Source this file in other scripts, then call:
#   process_ini_file '/path/to/file.conf'
#   value=$(get_value 'section_name' 'key_name')
#
# Optional global overrides (set before sourcing):
#   case_sensitive_sections=false
#   case_sensitive_keys=false
#   default_to_uppercase=true
#   show_config_warnings=false
#   show_config_errors=false

# ---------------------------------------------------------------------------
# Global Variables (can be overridden by the calling script before sourcing)
# ---------------------------------------------------------------------------
declare case_sensitive_sections
declare case_sensitive_keys
declare default_to_uppercase
declare show_config_warnings
declare show_config_errors

DEFAULT_SECTION='default'
sections=()

# ---------------------------------------------------------------------------
# Local defaults
# ---------------------------------------------------------------------------
local_case_sensitive_sections=true
local_case_sensitive_keys=true
local_default_to_uppercase=false
local_show_config_warnings=true
local_show_config_errors=true

# ---------------------------------------------------------------------------
# setup_global_variables — apply any caller overrides before parsing
# ---------------------------------------------------------------------------
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

    DEFAULT_SECTION=$(handle_default_case "${DEFAULT_SECTION}")
    sections+=("${DEFAULT_SECTION}")
}

# ---------------------------------------------------------------------------
# in_array — returns 0 if needle is in array, 1 otherwise
# ---------------------------------------------------------------------------
function in_array()
{
    local haystack="${1}[@]"
    local needle=${2}

    for i in ${!haystack}; do
        if [[ ${i} == "${needle}" ]]; then
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# show_warning / show_error — conditional output helpers
# ---------------------------------------------------------------------------
function show_warning()
{
    if [[ "${local_show_config_warnings}" = true ]]; then
        format=$1
        shift
        # shellcheck disable=SC2059
        printf "[ WARNING ] ${format}" "$@"
    fi
}

function show_error()
{
    if [[ "${local_show_config_errors}" = true ]]; then
        format=$1
        shift
        # shellcheck disable=SC2059
        printf "[ ERROR ] ${format}" "$@" >&2
    fi
}

# ---------------------------------------------------------------------------
# handle_default_case — lower or uppercase a string based on config
# ---------------------------------------------------------------------------
function handle_default_case()
{
    local str=$1

    if [[ "${local_default_to_uppercase}" = false ]]; then
        str=$(echo -e "${str}" | tr '[:upper:]' '[:lower:]')
    else
        str=$(echo -e "${str}" | tr '[:lower:]' '[:upper:]')
    fi
    echo "${str}"
}

# ---------------------------------------------------------------------------
# process_section_name — cleanse a section name from the INI file
# ---------------------------------------------------------------------------
function process_section_name()
{
    local section=$1

    section="${section##*( )}"                                                     # Remove leading spaces
    section="${section%%*( )}"                                                     # Remove trailing spaces
    section=$(echo -e "${section}" | tr -s '[:punct:] [:blank:]' '_')              # Punct/blank → underscore
    section=$(echo -e "${section}" | sed 's/[^a-zA-Z0-9_]//g')                    # Remove non-alphanumerics

    if [[ "${local_case_sensitive_sections}" = false ]]; then
        section=$(handle_default_case "${section}")
    fi
    echo "${section}"
}

# ---------------------------------------------------------------------------
# process_key_name — cleanse a key name from the INI file
# ---------------------------------------------------------------------------
function process_key_name()
{
    local key=$1

    key="${key##*( )}"                                                             # Remove leading spaces
    key="${key%%*( )}"                                                             # Remove trailing spaces
    key=$(echo -e "${key}" | tr -s '[:punct:] [:blank:]' '_')                     # Punct/blank → underscore
    key=$(echo -e "${key}" | sed 's/[^a-zA-Z0-9_]//g')                            # Remove non-alphanumerics

    if [[ "${local_case_sensitive_keys}" = false ]]; then
        key=$(handle_default_case "${key}")
    fi
    echo "${key}"
}

# ---------------------------------------------------------------------------
# process_value — cleanse a value from the INI file
# ---------------------------------------------------------------------------
function process_value()
{
    local value=$1

    value="${value%%\;*}"                                                          # Strip inline ; comments
    value="${value%%\#*}"                                                          # Strip inline # comments
    value="${value##*( )}"                                                         # Remove leading spaces
    value="${value%%*( )}"                                                         # Remove trailing spaces
    value=$(escape_string "${value}")

    echo "${value}"
}

# ---------------------------------------------------------------------------
# escape_string / unescape_string — protect single quotes from eval
# ---------------------------------------------------------------------------
function escape_string()
{
    local clean
    clean=${1//\'/SINGLE_QUOTE}
    echo "${clean}"
}

function unescape_string()
{
    local orig
    orig=${1//SINGLE_QUOTE/\'}
    echo "${orig}"
}

# ---------------------------------------------------------------------------
# process_ini_file — parse a file and load all sections/keys into memory
# Usage: process_ini_file '/etc/pve/scripts/client-setup.conf'
# ---------------------------------------------------------------------------
function process_ini_file()
{
    local line_number=0
    local section="${DEFAULT_SECTION}"
    local key_array_name=''

    setup_global_variables

    shopt -s extglob

    while IFS= read -r line || [[ -n "$line" ]]; do
        line=$(echo "$line" | tr -d '\r')                                          # Strip Windows carriage returns
        line_number=$((line_number + 1))

        # Skip comments and blank lines
        if [[ ${line} =~ ^# || ${line} =~ ^\; || -z ${line} ]]; then
            continue
        fi

        if [[ ${line} =~ ^"["(.+)"]"$ ]]; then
            # Section header
            section=$(process_section_name "${BASH_REMATCH[1]}")

            if ! in_array sections "${section}"; then
                eval "${section}_keys=()"
                eval "${section}_values=()"
                sections+=("${section}")
            fi

        elif [[ ${line} =~ ^(.*)"="(.*) ]]; then
            # key=value pair
            key=$(process_key_name "${BASH_REMATCH[1]}")
            value=$(process_value "${BASH_REMATCH[2]}")

            if [[ -z ${key} ]]; then
                show_error 'line %d: No key name\n' "${line_number}"
            else
                if [[ "${section}" == "${DEFAULT_SECTION}" ]]; then
                    show_warning '%s=%s - Defined on line %s before first section - added to "%s" group\n' \
                        "${key}" "${value}" "${line_number}" "${DEFAULT_SECTION}"
                fi

                eval key_array_name="${section}_keys"

                if in_array "${key_array_name}" "${key}"; then
                    show_warning 'key %s - Defined multiple times within section %s\n' "${key}" "${section}"
                fi

                eval "${section}_keys+=(${key})"
                eval "${section}_values+=('${value}')"
                eval "${section}_${key}='${value}'"
            fi
        fi
    done < "$1"
}

# ---------------------------------------------------------------------------
# get_value — retrieve a value by section and key
# Usage: vm_name=$(get_value 'c90001' 'vm_name')
# ---------------------------------------------------------------------------
function get_value()
{
    local section=''
    local key=''
    local keys=''
    local values=''

    section=$(process_section_name "${1}")
    key=$(process_key_name "${2}")
    section=$(handle_default_case "${section}")
    key=$(handle_default_case "${key}")

    eval "keys=( \"\${${section}_keys[@]}\" )"
    eval "values=( \"\${${section}_values[@]}\" )"

    for i in "${!keys[@]}"; do
        if [[ "${keys[${i}]}" = "${key}" ]]; then
            orig=$(unescape_string "${values[${i}]}")
            printf '%s' "${orig}"
        fi
    done
}

# ---------------------------------------------------------------------------
# display_config — dump all parsed sections/keys (useful for debugging)
# ---------------------------------------------------------------------------
function display_config()
{
    local section=''

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

# ---------------------------------------------------------------------------
# display_config_by_section — dump one section (useful for debugging)
# ---------------------------------------------------------------------------
function display_config_by_section()
{
    local section=$1
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
