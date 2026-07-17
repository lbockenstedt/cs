#!/bin/bash
# ini_parser_test.sh — regression test for clients ini-parser get_value.
#
# The expected values below are the captured output of the ORIGINAL
# (pre-fork-elimination) parser over the same sample INI, so this test pins the
# fork-free rewrite to the legacy behavior: name cleansing, inline comments,
# CRLF handling, single-quote escaping, first-match-wins on duplicate keys, and
# no stale/doubled values after a re-parse.
#
# Usage: bash clients/tests/ini_parser_test.sh [path-to-ini-parser.sh]
# Defaults to clients/lib/ini-parser.sh (falls back to clients/linux/).

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARSER="${1:-}"
if [[ -z "$PARSER" ]]; then
    if [[ -f "$HERE/../lib/ini-parser.sh" ]]; then
        PARSER="$HERE/../lib/ini-parser.sh"
    else
        PARSER="$HERE/../linux/ini-parser.sh"
    fi
fi

# shellcheck disable=SC1090
source "$PARSER"

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT
CONF="$TMPDIR_T/sample.conf"

# Sample INI: orphan key, duplicate key (first wins), padded key/values, a CRLF
# line, empty value, single quotes, inline ;/# comments, spaces in key + value,
# punctuation-heavy section name, bucket + username sections.
{
    printf 'orphan=value0\n'
    printf '[simulation]\n'
    printf 'kill_switch=off\n'
    printf 'kill_switch=on\n'
    printf 'rapid_update = on   \n'
    printf 'sim_load=42\r\n'
    printf 'web_server=on\n'
    printf 'empty_key=\n'
    printf "quoted=it's a test\n"
    printf 'inline=value ; trailing comment\n'
    printf 'inline2=value2 # trailing comment\n'
    printf 'spaced value = has spaces inside\n'
    printf '[Weird Section-Name!]\n'
    printf 'some key!=some value\n'
    printf '[address]\n'
    printf 'smb_address=//10.0.0.5/share\n'
    printf '[s3]\n'
    printf 'dns_fail=on\n'
    printf '[kbell]\n'
    printf 'simulation_id=s7\n'
    printf "ssidpw=pa'ss\n"
    printf '; comment\n'
    printf '# comment2\n'
} > "$CONF"

fails=0
checks=0

expect() {
    local section="$1" key="$2" want="$3" got
    got="$(get_value "$section" "$key")"
    checks=$((checks + 1))
    if [[ "$got" != "$want" ]]; then
        echo "FAIL: get_value '$section' '$key' -> [$got], want [$want]"
        fails=$((fails + 1))
    fi
}

run_all_checks() {
    expect default    orphan          'value0'
    # Duplicate key: FIRST occurrence wins (legacy array scan returned first match).
    expect simulation kill_switch     'off'
    expect simulation rapid_update    'on'
    # CRLF line: trailing \r stripped.
    expect simulation sim_load        '42'
    expect simulation web_server      'on'
    expect simulation empty_key       ''
    # Single quotes survive the SINGLE_QUOTE escape round-trip.
    expect simulation quoted          "it's a test"
    # Inline ;/# comments stripped, trailing spaces trimmed.
    expect simulation inline          'value'
    expect simulation inline2         'value2'
    # Key with a space is cleansed to underscore; value keeps inner spaces.
    expect simulation spaced_value    'has spaces inside'
    # Punctuation-heavy names: stored case-preserved, but get_value lowercases
    # the query (legacy behavior) so the mixed-case stored section is a miss.
    expect 'Weird Section-Name!' 'some key!' ''
    expect weird_section_name_   'some key!' ''
    expect address    smb_address     '//10.0.0.5/share'
    expect s3         dns_fail        'on'
    expect s3         missing_key     ''
    expect missing_section anything   ''
    expect kbell      simulation_id   's7'
    expect kbell      ssidpw          "pa'ss"
    # Case-insensitive lookup of lowercase-stored names (query is case-folded).
    expect SIMULATION KILL_SWITCH     'off'
}

process_ini_file "$CONF"
run_all_checks

# Re-parse the SAME file: values must not accumulate ("offoff" doubling bug)
# and every expectation must still hold.
process_ini_file "$CONF"
run_all_checks

# Re-parse a REDUCED file: keys removed from the config must go stale-free
# (a removed key returns empty, a changed value returns the new value).
printf '[simulation]\nkill_switch=on\n' > "$CONF"
process_ini_file "$CONF"
expect simulation kill_switch 'on'
expect simulation rapid_update ''
expect simulation quoted       ''
expect s3         dns_fail     ''

if [[ $fails -eq 0 ]]; then
    echo "OK: $checks checks passed ($PARSER)"
    exit 0
fi
echo "FAILED: $fails of $checks checks ($PARSER)"
exit 1
