#!/usr/bin/env python3
"""Build the jsonmend conformance corpus (corpus/cases/**.json).

Case format (one JSON file per case):

    {
      "input":     "<the broken JSON text>",
      "verdict":   "deterministic" | "ambiguous" | "unrecoverable",
      "expected":  <the single correct repair>          (deterministic)
      "accepted":  [<repair>, ...]                      (ambiguous)
      "check":     "value" (default) | "valid"          (optional)
      "rationale": "why",
      "source":    "jsonmend" | "json_repair-tests" | "jsonrepair-tests"
    }

Comparison semantics (see corpus/README.md): an implementation passes a
case when its output text parses as RFC 8259 JSON and the parsed value
matches (numbers compare mathematically; booleans are not numbers).
"""

import json
import os
import shutil
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "corpus")
CASES = os.path.join(ROOT, "cases")

D, A, U = "deterministic", "ambiguous", "unrecoverable"

# ---------------------------------------------------------------------------
# Hand-authored cases.
# Each entry: (name, input, verdict, payload, rationale, source[, check])
# payload: expected for D, accepted-list for A, None for U.
# ---------------------------------------------------------------------------

JR = "json_repair-tests"   # data point taken from mangiucugna/json_repair tests
JJ = "jsonrepair-tests"    # data point taken from josdejong/jsonrepair tests
JM = "jsonmend"

HAND = {}

HAND["valid"] = [
    ("object", '{"a": 1, "b": "x", "c": null, "d": false}', D,
     {"a": 1, "b": "x", "c": None, "d": False},
     "valid JSON must round-trip unchanged", JM),
    ("array", '[1, "hi", true, false, null, {}, []]', D,
     [1, "hi", True, False, None, {}, []],
     "valid JSON must round-trip unchanged", JJ),
    ("nested", '{"k1": {"k2": [1, 2, 3]}}', D, {"k1": {"k2": [1, 2, 3]}},
     "valid JSON must round-trip unchanged", JR),
    ("numbers", '[23, 0, 0.0, -0, 2.3, 2300e3, 2e-3, -2]', D,
     [23, 0, 0.0, 0, 2.3, 2300000.0, 0.002, -2],
     "all RFC 8259 number forms", JJ),
    ("bignum", '{"key": 12345678901234567890}', D,
     {"key": 12345678901234567890},
     "integers beyond 2^63 must not lose precision", JR),
    ("string-escapes", '"\\"\\\\\\/\\b\\f\\n\\r\\t"', D, "\"\\/\b\f\n\r\t",
     "all simple escapes", JJ),
    ("unicode-escape", '"\\u2605"', D, "★", "unicode escape decodes", JJ),
    ("delimiter-strings", '["[", "]", "{", "}", ":", ","]', D,
     ["[", "]", "{", "}", ":", ","],
     "JSON delimiters inside strings are content", JJ),
    ("empty-object", "{}", D, {}, "minimal object", JR),
    ("empty-array", "[]", D, [], "minimal array", JR),
    ("lone-true", "true", D, True, "bare literal", JJ),
    ("lone-null", "null", D, None, "bare literal", JJ),
    ("ws-only-object", "   {  }   ", D, {},
     "surrounding whitespace is ignored", JR),
    ("emoji", '"😀"', D, "😀", "astral plane chars are content", JJ),
    ("emoji-key", '{"😀": true}', D, {"😀": True},
     "astral plane chars in keys", JJ),
    ("cyrillic", '"йнформация"', D, "йнформация", "BMP unicode content", JJ),
]

HAND["truncation"] = [
    ("open-brace", "{", D, {}, "opened object, nothing else: empty object", JJ),
    ("open-bracket", "[", D, [], "opened array, nothing else: empty array", JJ),
    ("array-items", "[1, 2, 3", D, [1, 2, 3], "close the array", JR),
    ("array-trailing-comma", '["foo",', D, ["foo"],
     "trailing comma then EOF: drop the comma, close", JJ),
    ("object-after-value", '{"foo": "bar"', D, {"foo": "bar"},
     "close the object", JJ),
    ("string-value", '{"foo": "bar', D, {"foo": "bar"},
     "unterminated string keeps its content", JJ),
    ("after-colon", '{"foo":', D, {"foo": None},
     "key with no value: null is the JSON notion of absent", JJ),
    ("dangling-key", '{"foo"', D, {"foo": None},
     "complete key, no colon: pair with null", JJ),
    ("mid-key", '{"fo', D, {"fo": None},
     "truncated key keeps its prefix, pairs with null", JM),
    ("nested-trunc", '{"key1": {"key2": [1, 2, 3', D, {"key1": {"key2": [1, 2, 3]}},
     "close every open container", JR),
    ("string-with-comma", '{"text":"Hello Sergey,I hop', D,
     {"text": "Hello Sergey,I hop"},
     "EOF inside a string keeps everything: no comma splitting at EOF", JJ),
    ("many-commas-string", '{"message": "with, multiple, commma\'s, you see?',
     D, {"message": "with, multiple, commma's, you see?"},
     "EOF inside a string keeps everything", JJ),
    ("number-dot", "2.", D, 2.0, "complete the fraction with 0", JJ),
    ("number-exp", "2e", D, 2.0, "complete the exponent with 0", JJ),
    ("number-exp-plus", "2e+", D, 2.0, "complete the exponent with 0", JJ),
    ("partial-uescape", '{"foo":"bar\\u20', D, {"foo": "bar"},
     "incomplete \\u escape is dropped from a truncated string", JJ),
    ("partial-escape", '{"key": "val\\', D, {"key": "val"},
     "dangling backslash is dropped from a truncated string", JR),
    ("partial-true", '{"ok": tru', D, {"ok": True},
     "at EOF a unique prefix of a literal completes it", JM),
    ("partial-false", '[true, fal', D, [True, False],
     "at EOF a unique prefix of a literal completes it", JM),
    ("partial-null", '{"v": nu', D, {"v": None},
     "at EOF a unique prefix of a literal completes it", JM),
    ("employees", '{"employees":["John", "Anna",', D,
     {"employees": ["John", "Anna"]},
     "drop trailing comma, close all containers", JR),
    ("employees-string", '{"employees":["John", "Anna", "Peter', D,
     {"employees": ["John", "Anna", "Peter"]},
     "unterminated string keeps content, close all", JR),
    ("quote-only", '"', A, ["", None],
     "an opened empty string: empty string, or nothing mendable", JR),
    ("string-content", '"foo', D, "foo", "close the string", JJ),
    ("array-open-string", '["foo', D, ["foo"], "close string and array", JJ),
    ("array-quoted", '["foo"', D, ["foo"], "close the array", JJ),
    ("empty-key-value", '{"key": ""', D, {"key": ""},
     "value already complete: close object", JR),
    ("tool-call", '{"name": "search", "arguments": {"query": "weather in SF',
     D, {"name": "search", "arguments": {"query": "weather in SF"}},
     "the canonical LLM tool-call truncation", JM),
]

HAND["markdown-fence"] = [
    ("fenced", '```json\n{"a":"b"}\n```', D, {"a": "b"},
     "strip the fence, parse the payload", JJ),
    ("fenced-nolang", '```\n{"a":"b"}\n```', D, {"a": "b"},
     "language tag is optional", JJ),
    ("fenced-python-tag", '```python\n{"a":"b"}\n```', D, {"a": "b"},
     "any language tag is stripped", JJ),
    ("fence-unclosed", '```\n{"a":"b"}\n', D, {"a": "b"},
     "unclosed fence: parse to EOF", JJ),
    ("fence-close-only", '\n{"a":"b"}\n```', D, {"a": "b"},
     "stray closing fence is ignored", JJ),
    ("fence-tight", '```{"a":"b"}```', D, {"a": "b"},
     "fences without newlines", JJ),
    ("fence-array", '```\n[1,2,3]\n```', D, [1, 2, 3],
     "fenced array", JJ),
    ("prose-then-fence",
     "Based on the information extracted, here is the filled JSON output: "
     "```json { 'a': 'b' } ```", D, {"a": "b"},
     "prose before a fence is junk", JR),
    ("prose-multiline-fence",
     "\nThe next 64 elements are:\n```json\n{ \"key\": \"value\" }\n```",
     D, {"key": "value"},
     "multi-line prose before a fence is junk", JR),
    ("quad-backticks", '````{ "key": "value" }```', D, {"key": "value"},
     "sloppy fence lengths still strip", JR),
    ("trailing-fence-junk", '{    "a": "",    "b": [ { "c": 1} ] \n}```', D,
     {"a": "", "b": [{"c": 1}]},
     "stray fence after a value is junk", JR),
    ("two-fences",
     'lorem ```json {"key":"value"} ``` ipsum ```json [1,2,3,true] ``` 42',
     A, [[{"key": "value"}, [1, 2, 3, True]], {"key": "value"}],
     "multiple fenced payloads: an array of both, or the first only; "
     "prose between and after fences is junk", JR),
    ("fence-in-string", '{"key": "```json"', D, {"key": "```json"},
     "backticks inside a string are content", JR),
    ("backtick-pair-in-string", '{"key": "``"', D, {"key": "``"},
     "backticks inside a string are content", JR),
    ("fenced-truncated", '```json\n{"items": [1, 2', D, {"items": [1, 2]},
     "fence + truncation compose", JM),
    ("decision-prose",
     "**Decision**: bla, bla (some clarification):\n\n```json\n"
     "{\n  \"key\": \"value\"\n}\n```\n", D, {"key": "value"},
     "parenthesized prose must not hijack the fenced payload", JR),
    ("numbered-prose",
     "(1) Keep this note in the explanation.\n\n```json\n"
     "{\n  \"key\": \"value\"\n}\n```\n", D, {"key": "value"},
     "numbered prose must not hijack the fenced payload", JR),
    ("fenced-tuple", 'Here is the tuple payload:\n\n```json\n(1, 2)\n```\n',
     D, [1, 2], "a parenthesized tuple inside a fence is an array", JR),
]

HAND["quotes"] = [
    ("single-quotes", "{'a': 2}", D, {"a": 2},
     "single quotes become double quotes", JJ),
    ("single-quoted-strings", "{'a':'foo'}", D, {"a": "foo"},
     "single quotes become double quotes", JJ),
    ("mixed-quotes", '{"a":\'foo\'}', D, {"a": "foo"},
     "mixed quote styles normalize", JJ),
    ("smart-quotes", '{“a”:“b”}', D, {"a": "b"},
     "curly quotes act as quotes", JJ),
    ("smart-quotes-single", '{‘a’:‘b’}', D, {"a": "b"},
     "curly single quotes act as quotes", JJ),
    ("backtick-quotes", '{`a´:`b´}', D, {"a": "b"},
     "backtick/acute pairs act as quotes", JJ),
    ("smart-inside-normal", '"Rounded “ quote"', D, "Rounded “ quote",
     "smart quotes inside a closed string are content", JJ),
    ("smart-inside-single", "'Rounded ’ quote'", D, "Rounded ’ quote",
     "smart quotes inside a closed string are content", JJ),
    ("double-inside-single", "'Double \" quote'", D, 'Double " quote',
     "double quote inside single-quoted string is content", JJ),
    ("apostrophe", '{"text": "The quick brown fox won\'t jump"}', D,
     {"text": "The quick brown fox won't jump"},
     "apostrophes inside double-quoted strings are content", JR),
    ("missing-end-quote", '{"a":"b}', D, {"a": "b"},
     "unterminated string closed by structure", JJ),
    ("missing-end-quote-pair", '{"a":"b,"c":"d"}', D, {"a": "b", "c": "d"},
     "the `,\"...\":` pattern reopens as the next member", JJ),
    ("missing-start-quote", 'abc"', D, "abc",
     "stray trailing quote: the text is the string", JJ),
    ("missing-start-key", '{a":"foo","b":"bar"}', D,
     {"a": "foo", "b": "bar"}, "key missing its open quote", JJ),
    ("missing-start-value", '{"a":foo","b":"bar"}', D,
     {"a": "foo", "b": "bar"}, "value missing its open quote", JJ),
    ("inner-quotes", '{"key": "apple "bee" carrot"}', D,
     {"key": 'apple "bee" carrot'},
     "unescaped quotes inside a string stay content when no structure "
     "follows them", JJ),
    ("inner-quotes-array", '["lorem "ipsum" sic"]', D, ["lorem \"ipsum\" sic"],
     "unescaped quotes inside an array string stay content", JR),
    ("tv-screen", '"The TV has a 24" screen"', D, 'The TV has a 24" screen',
     "unescaped quote inside string is content", JJ),
    ("escaped-quotes", '{"foo": "\\"bar\\""', D, {"foo": '"bar"'},
     "escaped quotes inside string survive", JR),
    ("html-attr",
     '{\n"html": "<h3 id="aaa">Waarom meer dan 200 Technical Experts - '
     '"Passie voor techniek"?</h3>"}', D,
     {"html": '<h3 id="aaa">Waarom meer dan 200 Technical Experts - '
              '"Passie voor techniek"?</h3>'},
     "HTML attributes with unescaped quotes stay content", JR),
    ("missing-quote-newline-split", '[\n"abc,\n"def"\n]', D, ["abc", "def"],
     "a quote opening the next line closes the unterminated string at the "
     "newline; its trailing comma is the separator", JJ),
    ("missing-quote-bracket-line", '["abc]\n', A, [["abc"], ["abc]"]],
     "either the ] closes the array (string was unterminated) or it is "
     "content of an unterminated string", JJ),
    ("url-missing-end", '{"url":"https://www.bible.com/}', D,
     {"url": "https://www.bible.com/"},
     "unterminated URL string closed by structure", JJ),
    ("url-missing-end-comma", '{"url":"https://www.bible.com/,"id":2}', D,
     {"url": "https://www.bible.com/", "id": 2},
     "`,\"...\":`-pattern reopens as next member", JJ),
    ("colon-in-string", '"12:20', D, "12:20",
     "colons inside unterminated strings are content", JJ),
    ("time-value", '{"time":"12:20}', D, {"time": "12:20"},
     "colons inside unterminated string values are content", JJ),
    ("she-said", '{"text": "She said:', D, {"text": "She said:"},
     "colon at end of truncated string is content", JJ),
    ("escaped-string-doc", '\\"hello world\\"', D, "hello world",
     "a backslash-escaped JSON document is unwrapped", JJ),
    ("escaped-string-object", '{\\"key\\": \\"value\\"}', D,
     {"key": "value"}, "backslash-escaped keys/values are unwrapped", JR),
    ("double-double-quotes", '{""answer"":[{""traits"":\'\'Female aged 60+\'\','
     '""answer1"":""5""}]}', D,
     {"answer": [{"traits": "Female aged 60+", "answer1": "5"}]},
     "doubled quotes collapse", JR),
    ("key-quote-inside", '{"k"e"y": "value"}', D, {'k"e"y': "value"},
     "unescaped quotes inside a key are content", JR),
    ("string-not-comment", '"/* foo */"', D, "/* foo */",
     "comment syntax inside a string is content", JJ),
    ("string-not-trailing-comma", '"[1,2,3,]"', D, "[1,2,3,]",
     "array syntax inside a string is content", JJ),
]

HAND["keys"] = [
    ("unquoted-key", "{a:2}", D, {"a": 2}, "quote bare keys", JJ),
    ("unquoted-keys-multi", "{key:value,key2:value2}", D,
     {"key": "value", "key2": "value2"},
     "bare keys and bare values both quote", JR),
    ("numeric-key", "{2: 2}", D, {"2": 2},
     "non-string keys stringify (JSON keys are strings)", JJ),
    ("true-key", "{true: 2}", D, {"true": 2},
     "non-string keys stringify", JJ),
    ("numeric-key-mixed", '{"key": "value", 5: "value"}', D,
     {"key": "value", "5": "value"}, "non-string keys stringify", JR),
    ("missing-colon", '{"a" "b"}', D, {"a": "b"},
     "missing colon between key and value", JJ),
    ("missing-colon-number", '{"a" 2}', D, {"a": 2},
     "missing colon before number", JJ),
    ("missing-colon-glued", '{"a"2}', D, {"a": 2},
     "missing colon, no space", JJ),
    ("missing-colon-bare", "{a 'b'}", D, {"a": "b"},
     "bare key, missing colon", JJ),
    ("key-without-value-comma", '{"a",}', A, [{"a": None}, {}, ["a"]],
     "a lone key: pair with null, drop it, or read as array", JJ),
    ("empty-key", '{"": "value"', D, {"": "value"},
     "empty string is a legal key", JR),
    ("empty-single-key", "{'': 1}", D, {"": 1},
     "empty single-quoted key", JR),
    ("dup-keys", '[{"b":"v1","b":"v2"}]', A, [[{"b": "v2"}], [{"b": "v1"}]],
     "duplicate keys: last-wins (json.loads convention) or first-wins", JR),
    ("key-newline", '{"key_1\\n": "value"}', A,
     [{"key_1": "value"}, {"key_1\n": "value"}],
     "raw newline in key: strip it or keep it escaped", JR),
    ("half-quoted-key", '{"key:value}', D, {"key": "value"},
     "colon inside an unterminated key splits key from value", JR),
    ("key-colon-quote", '{"key:"value"}', D, {"key": "value"},
     "quote after colon belongs to the value", JR),
    ("missing-comma-keys", '{"a":2\n"b":3\n}', D, {"a": 2, "b": 3},
     "newline between members implies the comma", JJ),
    ("missing-comma-bare", '{"a":2\n"b":3\nc:4}', D, {"a": 2, "b": 3, "c": 4},
     "newline-separated members, mixed quoting", JJ),
    ("first-last", '{\n  "firstName": "John"\n  lastName: Smith', D,
     {"firstName": "John", "lastName": "Smith"},
     "missing comma + bare key + bare value + truncation", JJ),
    ("object-continuation", '{"key": "value"}, "key2": "value2"}', D,
     {"key": "value", "key2": "value2"},
     "a `}, \"key\":` continuation reopens the object", JR),
    ("object-continuation-chain",
     '{"key1": "value1"}, "key2": "value2", "key3": "value3"}', D,
     {"key1": "value1", "key2": "value2", "key3": "value3"},
     "continuation chains", JR),
]

HAND["literals"] = [
    ("python-true", "{'b': True}", D, {"b": True}, "Python booleans", JR),
    ("python-caps", '{"key": TRUE, "key2": FALSE, "key3": Null}   ', D,
     {"key": True, "key2": False, "key3": None},
     "any-case boolean/null literals", JR),
    ("python-none", '{"v": None}', D, {"v": None}, "Python None", JJ),
    ("top-true", "True", D, True, "bare Python literal", JJ),
    ("top-false", "False", D, False, "bare Python literal", JJ),
    ("top-none", "None", D, None, "bare Python literal", JJ),
    ("undefined", '{"a":undefined}', D, {"a": None},
     "JS undefined becomes null", JJ),
    ("undefined-array", "[undefined]", D, [None],
     "JS undefined becomes null", JJ),
    ("nan", '{"a": NaN}', A, [{"a": None}, {"a": "NaN"}],
     "NaN is not JSON: null (JSON.stringify convention) or the string", JM),
    ("infinity", '[Infinity, -Infinity]', A,
     [[None, None], ["Infinity", "-Infinity"]],
     "Infinity is not JSON: null or the string", JM),
    ("literal-like-string", '{"key": Truelove}', D, {"key": "Truelove"},
     "words merely starting with a literal stay strings", JM),
    ("literal-then-prose", '{"a": null pointer}', D, {"a": "null pointer"},
     "a literal followed by more words is one unquoted string", JM),
    ("bare-word", "foo", A, ["foo", ""],
     "a bare word: the string itself, or nothing mendable", JJ),
    ("bare-words", "hello   world", A, ["hello   world", ""],
     "bare words become one string, or nothing mendable", JJ),
    ("unquoted-value", '{"name": John}', D, {"name": "John"},
     "bare value quotes", JR),
    ("unquoted-multiword", '{"city": New York}', D, {"city": "New York"},
     "bare multi-word value quotes whole", JR),
    ("unquoted-with-punct", "{greeting: hello world!}", D,
     {"greeting": "hello world!"}, "punctuation stays in bare values", JJ),
    ("unquoted-array", "[a,b]", D, ["a", "b"], "bare array items quote", JJ),
    ("unquoted-url-value", "{url:https://www.bible.com/}", D,
     {"url": "https://www.bible.com/"},
     "URLs survive bare (no // comment confusion)", JJ),
    ("unquoted-url-array", "[https://www.bible.com/,2]", D,
     ["https://www.bible.com/", 2], "URLs survive bare in arrays", JJ),
    ("date-value", '{"date":2024-10-18T18:35:22.229Z}', D,
     {"date": "2024-10-18T18:35:22.229Z"},
     "datetime-like bare tokens become strings", JJ),
    ("uuid-value", '{"rowId": 57eeeeb1-450b-482c-81b9-4be77e95dee2}', D,
     {"rowId": "57eeeeb1-450b-482c-81b9-4be77e95dee2"},
     "uuid-like bare tokens become strings", JR),
    ("braces-in-bare", "{text:words{words in brackets}more words}", D,
     {"text": "words{words in brackets}more words"},
     "balanced braces inside bare values are content", JR),
    ("regex-value", "{regex: /standalone-styles.css/}", D,
     {"regex": "/standalone-styles.css/"},
     "regex literals become strings", JJ),
]

HAND["comments"] = [
    ("block-before", '/* foo */ {}', D, {}, "leading block comment", JJ),
    ("block-after", '{} /* foo */ ', D, {}, "trailing block comment", JJ),
    ("block-unclosed", '{} /* foo ', D, {}, "unclosed trailing comment", JJ),
    ("block-between", '{"a":"foo",/*hello*/"b":"bar"}', D,
     {"a": "foo", "b": "bar"}, "comment between members", JJ),
    ("block-before-value", '{"flag":/*boolean*/true}', D, {"flag": True},
     "comment between colon and value", JJ),
    ("line-comment", '{\n"a":"foo",//hello\n"b":"bar"\n}', D,
     {"a": "foo", "b": "bar"}, "line comment between members", JJ),
    ("hash-comment", '{ "key": { "key2": "value2" # comment }, "key3": "value3" }',
     A, [{"key": {"key2": "value2"}, "key3": "value3"},
         {"key": {"key2": "value2"}}],
     "# comment runs to end of line; whether the } inside it counts is "
     "implementation-defined", JR),
    ("line-comment-value", '{ "key": { "key2": "value2" // comment\n}, '
     '"key3": "value3" }', D,
     {"key": {"key2": "value2"}, "key3": "value3"},
     "// comment runs to end of line", JR),
    ("comment-in-array", '[ "value", /* comment */ "value2" ]', D,
     ["value", "value2"], "block comment between elements", JR),
    ("comment-truncated", '{ "key": "value" /* comment', D,
     {"key": "value"}, "unclosed comment at EOF", JR),
    ("comment-bracket-shield", "{\n// comment ]\n}", D, {},
     "brackets inside comments are not structure", JR),
    ("comment-block-bracket", "{/* comment ] */}", D, {},
     "brackets inside block comments are not structure", JR),
    ("comments-actions", """
    {
        "Changes": [
            //object a
            {
                "Action": "1"
            },
            //object b ]
            {
                "Action": "2"
            },
            //object c ]
            {
                "Action": "3"
            }
        ]
    }
    """, D, {"Changes": [{"Action": "1"}, {"Action": "2"}, {"Action": "3"}]},
     "line comments with brackets between array items", JR),
    ("string-keeps-comment", '["a"/* foo */]', D, ["a"],
     "comment directly after a string closes it", JJ),
    ("comment-many-lines", ("# comment\n" * 50) + '{"key": "value"}', D,
     {"key": "value"}, "many leading comment lines must not recurse", JR),
]

HAND["commas"] = [
    ("trailing-array", "[1,2,3,]", D, [1, 2, 3], "trailing comma drops", JJ),
    ("trailing-object", '{"a":2,}', D, {"a": 2}, "trailing comma drops", JJ),
    ("trailing-nested", '{"array":[1,2,3,]}', D, {"array": [1, 2, 3]},
     "nested trailing comma drops", JJ),
    ("trailing-top", "4,", D, 4, "top-level trailing comma drops", JJ),
    ("leading-array", "[,1,2,3]", D, [1, 2, 3], "leading comma drops", JJ),
    ("leading-object", '{,"message": "hi"}', D, {"message": "hi"},
     "leading comma drops", JJ),
    ("double-comma", "[1,,2]", D, [1, 2], "empty slots collapse", JM),
    ("missing-array", '{"array": [{}{}]}', D, {"array": [{}, {}]},
     "missing comma between array items", JJ),
    ("missing-array-nl", '{"array": [\n1\n2\n]}', D, {"array": [1, 2]},
     "newline-separated array items", JJ),
    ("missing-array-strings", '{"array": [\n"a"\n"b"\n]}', D,
     {"array": ["a", "b"]}, "newline-separated string items", JJ),
    ("missing-after-string", '["a" 2]', D, ["a", 2],
     "number after closed string implies comma", JJ),
    ("missing-object-value", '{"key": value "key2" : "value2" ', D,
     {"key": "value", "key2": "value2"},
     "bare value then quoted key: comma implied", JR),
    ("missing-strings-inline", '{"key": ["value" "value1" "value2"]}', D,
     {"key": ["value", "value1", "value2"]},
     "quote-to-quote adjacency implies commas", JR),
    ("missing-numbers", "[105,12", D, [105, 12], "truncated number array", JR),
    ("ellipsis-array", "[1,2,3,...]", D, [1, 2, 3],
     "ellipsis placeholder drops", JR),
    ("ellipsis-mid", "[1, 2, ... , 3]", D, [1, 2, 3],
     "mid-array ellipsis drops", JR),
    ("ellipsis-string", "[1, 2, '...', 3]", D, [1, 2, "...", 3],
     "quoted ellipsis is content", JR),
    ("ellipsis-object", '{"a":2,"b":3,...}', D, {"a": 2, "b": 3},
     "object ellipsis drops", JJ),
    ("ellipsis-object-mid", '{"a":2,"b":3,...,"z":26}', D,
     {"a": 2, "b": 3, "z": 26}, "mid-object ellipsis drops", JJ),
    ("ellipsis-only", "[...]", D, [], "only ellipsis: empty array", JJ),
    ("missing-value-comma", '{"key": , "key2": "value2"}', A,
     [{"key": None, "key2": "value2"}, {"key": "", "key2": "value2"}],
     "missing value: null (absent) or empty string", JR),
    ("missing-value-end", '{"a":}', A, [{"a": None}, {"a": ""}],
     "missing value: null (absent) or empty string", JJ),
    ("missing-value-mid", '{"a":,"b":2}', A,
     [{"a": None, "b": 2}, {"a": "", "b": 2}],
     "missing value: null (absent) or empty string", JJ),
    ("semicolon-sep", '[1; 2; 3]', D, [1, 2, 3],
     "semicolons act as commas", JM),
]

HAND["structure"] = [
    ("redundant-close", '{"a": 1}}', D, {"a": 1},
     "extra closers after a complete value are junk", JJ),
    ("redundant-close-many", '{"a": 1}}]}', D, {"a": 1},
     "extra closers after a complete value are junk", JJ),
    ("wrong-close-array", '{"a":2]', D, {"a": 2},
     "] closes an object when no array is open", JJ),
    ("wrong-close-comma", '{"a":2,]', D, {"a": 2},
     "trailing comma + wrong closer", JJ),
    ("wrong-close-object", "[2,}", D, [2],
     "} closes an array when no object is open", JJ),
    ("bracket-brace", "[}", D, [], "mismatched empty containers", JJ),
    ("brace-bracket", "{]", D, {}, "mismatched empty containers", JJ),
    ("array-in-object-close", '{"array":[{"key": "value"], "key2": "value2"}',
     D, {"array": [{"key": "value"}], "key2": "value2"},
     "] closes both the inner object and the array", JR),
    ("nested-wrong-close", '{"key1": ["value1", "value2"}, "key2": ["value3", "value4"]}',
     D, {"key1": ["value1", "value2"], "key2": ["value3", "value4"]},
     "} closes the array, object continues", JR),
    ("missing-open-brace", '[{"i":1{"i":2}]', D, [{"i": 1}, {"i": 2}],
     "a { at member position starts the next element", JJ),
    ("missing-open-brace-comma", '[{"i":1,{"i":2}]', D, [{"i": 1}, {"i": 2}],
     "a { after comma starts the next element", JJ),
    ("extra-close-between", '[{"key":"value"}},{"key":"value"}]', D,
     [{"key": "value"}, {"key": "value"}],
     "doubled closer between elements is junk", JR),
    ("array-key-colon", '["key":"value"}]', D, [{"key": "value"}],
     "key/colon inside an array starts an object", JR),
    ("array-key-colon-clean", '["key":"value"]', D, [{"key": "value"}],
     "key/colon inside an array starts an object", JR),
    ("set-literal", "{'item1', 'item2', 'item3'}", A,
     [["item1", "item2", "item3"],
      {"item1": None, "item2": None, "item3": None}],
     "a Python set literal: array of items, or null-valued members", JR),
    ("tuple", '("a", "b", "c")', D, ["a", "b", "c"],
     "Python tuple becomes an array", JR),
    ("tuple-nested", "((1, 2), (3, 4))", D, [[1, 2], [3, 4]],
     "nested tuples become arrays", JR),
    ("tuple-value", '{"coords": (1, 2), "ok": true}', D,
     {"coords": [1, 2], "ok": True}, "tuple values become arrays", JR),
    ("tuple-empty", '{"empty": ()}', D, {"empty": []},
     "empty tuple becomes empty array", JR),
    ("paren-scalar", "(1)", D, 1,
     "a parenthesized scalar stays scalar", JR),
    ("paren-scalar-string", '("x")', D, "x",
     "a parenthesized scalar stays scalar", JR),
    ("jsonp", "callback_123({});", D, {},
     "JSONP wrapper strips", JJ),
    ("jsonp-array", "callback_123([1,2]);", D, [1, 2],
     "JSONP wrapper strips", JJ),
    ("jsonp-scalar", 'callback_123("foo");', D, "foo",
     "JSONP wrapper strips", JJ),
    ("mongo-objectid", '{"_id":ObjectId("123")}', D, {"_id": "123"},
     "MongoDB type wrappers unwrap", JJ),
    ("mongo-numberlong", 'NumberLong("2")', D, "2",
     "MongoDB type wrappers unwrap", JJ),
    ("mongo-isodate", '{"d" : ISODate("2012-12-19T06:01:17.171Z")}', D,
     {"d": "2012-12-19T06:01:17.171Z"}, "MongoDB type wrappers unwrap", JJ),
    ("split-array-merge", '{ "key": ["a"], ["b"], ["c"], "key3": "value3" }',
     A, [{"key": ["a", "b", "c"], "key3": "value3"},
         {"key": ["a"], "key3": "value3"}],
     "stray arrays after an array member: merge into it, or drop", JR),
]

HAND["concat"] = [
    ("multiple-values", '{"key":"value"}[1,2,3,true]', D,
     [{"key": "value"}, [1, 2, 3, True]],
     "multiple top-level values become an array", JR),
    ("newline-separated", "1\n2", D, [1, 2],
     "newline-separated values become an array", JJ),
    ("comma-separated", "1,2,3", D, [1, 2, 3],
     "comma-separated values become an array", JJ),
    ("ndjson", '{"a":1}\n{"b":2}\n{"c":3}\n', D,
     [{"a": 1}, {"b": 2}, {"c": 3}],
     "NDJSON becomes an array", JJ),
    ("ndjson-comments", '/* 1 */\n{}\n\n/* 2 */\n{}\n\n/* 3 */\n{}\n', D,
     [{}, {}, {}], "comment-separated NDJSON becomes an array", JJ),
    ("string-concat", '"hello" + " world"', D, "hello world",
     "JS string concatenation evaluates", JJ),
    ("string-concat-multi", '"a"+"b"+"c"', D, "abc",
     "chained concatenation evaluates", JJ),
    ("string-concat-nl", '"hello" +\n " world"', D, "hello world",
     "concatenation across newlines", JJ),
    ("string-concat-comment", '"hello" + /*comment*/ " world"', D,
     "hello world", "concatenation with comments", JJ),
    ("string-concat-value", "{\n  \"greeting\": 'hello' +\n 'world'\n}", D,
     {"greeting": "helloworld"}, "concatenation in a value", JJ),
    ("concat-truncated", '"hello +', D, "hello",
     "dangling + after unterminated string drops", JJ),
    ("empty-then-object", "[]{}", A, [[[], {}], []],
     "two top-level values: array of both, or the first", JR),
    ("prose-then-object", "stringbeforeobject {}", D, {},
     "prose before a structured value is junk", JR),
    ("object-then-prose", '{"a": 1} some trailing words', D, {"a": 1},
     "prose after a structured value is junk", JM),
]

HAND["numbers"] = [
    ("underscores", '{"value": 82_461_110}', D, {"value": 82461110},
     "digit-group underscores strip", JR),
    ("underscore-float", '{"value": 1_234.5_6}', D, {"value": 1234.56},
     "digit-group underscores strip", JR),
    ("leading-dot", '{"key": .25}', D, {"key": 0.25},
     "bare leading-dot fraction", JR),
    ("fraction", '{"key": 1/3}', D, {"key": "1/3"},
     "fractions are not JSON numbers: keep as string", JR),
    ("fraction-mid", '{"here": "now", "key": 1/3, "foo": "bar"}', D,
     {"here": "now", "key": "1/3", "foo": "bar"},
     "fractions are not JSON numbers: keep as string", JR),
    ("range", '{"key": 10-20}', D, {"key": "10-20"},
     "ranges are not JSON numbers: keep as string", JR),
    ("version", '{"v": 1.1.1}', D, {"v": "1.1.1"},
     "versions are not JSON numbers: keep as string", JR),
    ("version-top", "0.0.1", D, "0.0.1",
     "versions are not JSON numbers: keep as string", JJ),
    ("leading-zero", "0789", D, "0789",
     "leading-zero numbers are not JSON: keep as string (ZIP codes!)", JJ),
    ("leading-zeros", "000789", D, "000789",
     "leading-zero numbers keep as string", JJ),
    ("leading-zero-array", "[0789]", D, ["0789"],
     "leading-zero numbers keep as string", JJ),
    ("leading-zero-value", "{value:0789}", D, {"value": "0789"},
     "leading-zero numbers keep as string", JJ),
    ("es2020", "ES2020", A, ["ES2020", ""],
     "letters+digits is a bare word", JJ),
    ("double-dots", "234..5", D, "234..5",
     "malformed numbers keep as string", JJ),
    ("exp-then-dot", "2e3.4", D, "2e3.4",
     "malformed numbers keep as string", JJ),
    ("number-then-word", '{"key": 1notanumber }', D,
     {"key": "1notanumber"}, "number glued to word is a string", JR),
    ("trailing-dot", '{"key": 1. }', D, {"key": 1.0},
     "complete the fraction", JR),
    ("bare-exp", '{"key": 1e10 }', D, {"key": 10000000000.0},
     "exponent numbers parse", JR),
    ("incomplete-exp", '{"key": 1e }', A, [{"key": 1.0}, {"key": 1}],
     "dangling exponent: complete with e0", JR),
    ("minus-space", "[- ", D, [],
     "a lone minus is nothing", JR),
    ("plus-number", '{"n": +42}', D, {"n": 42},
     "leading plus strips", JM),
    ("unicode-minus", '{"n": −5}', D, {"n": -5},
     "U+2212 minus normalizes", JM),
]

HAND["unicode"] = [
    ("bom", '﻿{"a": 1}', D, {"a": 1}, "UTF-8 BOM is ignored", JM),
    ("bom-mid", '{"a":﻿"foo"}', D, {"a": "foo"},
     "stray BOM between tokens is whitespace", JJ),
    ("nbsp-ws", '{"a": "foo bar"}', D, {"a": "foo bar"},
     "NBSP is whitespace between tokens, content inside strings", JJ),
    ("zero-width", '{"a":​"foo"}', D, {"a": "foo"},
     "zero-width space between tokens is whitespace", JJ),
    ("ideographic-space", '{"a":　"foo"}', D, {"a": "foo"},
     "ideographic space between tokens is whitespace", JJ),
    ("escaped-pair", '"\\ud83d\\ude00"', D, "😀",
     "escaped surrogate pairs combine", JJ),
    ("lone-high-surrogate", '"a\\ud800b"', A,
     ["a�b", "a\ud800b", "ab"],
     "a lone surrogate cannot encode to UTF-8: replace it, pass it "
     "through, or drop it", JM),
    ("lone-surrogate-trunc", '{"s": "x\\ud83d', D, {"s": "x"},
     "truncated surrogate pair drops cleanly", JJ),
    ("control-chars", '"hello\nworld"', D, "hello\nworld",
     "raw control characters inside strings are content (escaped on "
     "output)", JJ),
    ("tab-in-key", '{"key\t_": "value"}', D, {"key\t_": "value"},
     "raw tab inside a key is content", JR),
    ("escaped-unicode-seq",
     '"\\u0439\\u043d\\u0444\\u043e\\u0440\\u043c\\u0430\\u0446\\u0438\\u044f"',
     D, "йнформация", "escaped unicode sequences decode", JJ),
    ("cjk-content", "{'test_中国人_ascii':'统一码'}", D,
     {"test_中国人_ascii": "统一码"}, "CJK content survives", JR),
    ("cjk-unquoted", '{"city": 北京}', D, {"city": "北京"},
     "bare CJK values become strings", JM),
    ("emoji-unquoted", '{"mood": 🎉}', A, [{"mood": "🎉"}, {"mood": ""}],
     "bare emoji value: string or unparseable", JM),
    ("null-escape", '"a\\u0000b"', D, "a\x00b",
     "escaped NUL decodes (it is valid JSON)", JM),
    ("fullwidth-colon", '{"a"：1}', A, [{"a": 1}, {"a": "：1"}],
     "fullwidth colon: separator or content", JM),
]

HAND["escapes"] = [
    ("invalid-escape", '"\\a"', D, "a",
     "unknown escape: drop the backslash, keep the char", JJ),
    ("escaped-newline", '"first\\\nsecond"', D, "first\nsecond",
     "backslash-newline is a line continuation", JJ),
    ("single-quote-escape", '"valu\\\'e"', D, "valu'e",
     "\\' is not JSON but means a plain apostrophe", JR),
    ("escape-in-single", "{'key': 'va\\'lue'}", D, {"key": "va'lue"},
     "escapes work inside single-quoted strings", JR),
    ("unicode-escapes-mixed", '{"key": "\\u0076\\u0061\\u006C\\u0075\\u0065"}',
     D, {"key": "value"}, "unicode escapes decode", JR),
    ("tab-escape-kept", '{"a": "x\\ty"}', D, {"a": "x\ty"},
     "valid escapes survive", JM),
    ("backslash-x", '"\\x41"', A, ["x41", "A", "\\x41"],
     "\\x is not JSON: drop backslash, decode as hex, or keep", JM),
    ("stringified-json-value", '{\'key\': "{\\"key\\": 1, \\"key2\\": 1}"}',
     D, {"key": '{"key": 1, "key2": 1}'},
     "escaped JSON inside a string stays a string", JR),
    ("latex", '{ "key": "x [0,2] f(-\\\\frac{3}{4})" }', D,
     {"key": "x [0,2] f(-\\frac{3}{4})"},
     "LaTeX backslashes survive as content", JR),
    ("windows-path", '{"path": "C:\\\\Users\\\\test"}', D,
     {"path": "C:\\Users\\test"}, "escaped backslashes decode", JM),
    ("real-newline-in-value", '{"text": "line1\nline2"}', D,
     {"text": "line1\nline2"},
     "raw newline inside a closed string is content", JM),
]

HAND["llm-output"] = [
    ("sure-heres", 'Sure! Here is the JSON you asked for:\n\n'
     '{"answer": 42, "confidence": "high"}', D,
     {"answer": 42, "confidence": "high"},
     "chatty preamble before a bare object", JM),
    ("apology-prefix", "I apologize for the confusion. The correct JSON "
     "is:\n```json\n{\"status\": \"ok\"}\n```", D, {"status": "ok"},
     "apology preamble + fence", JM),
    ("trailing-explanation", '{"result": [1, 2, 3]}\n\nThis array contains '
     "the first three integers as requested.", D, {"result": [1, 2, 3]},
     "trailing explanation after the value is junk", JM),
    ("cot-then-fence", "Let me think step by step.\n1. The user wants x.\n"
     "2. Therefore:\n```json\n{\"x\": true}\n```\nDone!", D, {"x": True},
     "chain-of-thought lines then a fenced value", JM),
    ("tool-args-fragment", '"location": "Paris", "unit": "celsius"}', A,
     [{"location": "Paris", "unit": "celsius"},
      ["location", "Paris, \"unit\": \"celsius\""]],
     "an object body missing its opening brace", JM),
    ("double-fenced", "```json\n{\"a\": 1}\n```\n\nWait, I need to correct "
     "that:\n\n```json\n{\"a\": 2}\n```", A,
     [[{"a": 1}, {"a": 2}], {"a": 1}, {"a": 2}],
     "self-correction: both payloads, the first, or the last", JM),
    ("yaml-drift", "name: test\nvalue: 42", A,
     ["name: test\nvalue: 42", {"name": "test", "value": 42}, ""],
     "YAML-style output: prose string, parsed object, or nothing", JM),
    ("html-content", '{"html": "<div class=\\"box\\">hi</div>"}', D,
     {"html": '<div class="box">hi</div>'},
     "escaped HTML survives", JM),
    ("markdown-content", '{"md": "# Title\\n\\n- item 1\\n- item 2"}', D,
     {"md": "# Title\n\n- item 1\n- item 2"},
     "markdown inside a string is content", JM),
    ("cjk-prose-fence", "好的，这是您要的 JSON：\n```json\n"
     "{\"城市\": \"北京\", \"人口\": 2154}\n```", D,
     {"城市": "北京", "人口": 2154},
     "CJK preamble + CJK content", JM),
    ("emoji-prose", "Here you go 🎉\n{\"done\": true}", D, {"done": True},
     "emoji in the preamble is junk", JM),
    ("sse-fragment", 'data: {"delta": {"content": "hel', A,
     [{"delta": {"content": "hel"}}, "data: {\"delta\": {\"content\": \"hel"],
     "an SSE-framed truncated event: parse the payload or keep the line",
     JM),
    ("function-call",
     '{"name": "send_email", "arguments": "{\\"to\\": \\"a@b.c\\", '
     '\\"subject\\": \\"Hi\\", \\"body\\": \\"Hello', D,
     {"name": "send_email",
      "arguments": '{"to": "a@b.c", "subject": "Hi", "body": "Hello'},
     "truncated stringified arguments stay a (truncated) string", JM),
    ("repeated-key-stream", '{"text": "a"}{"text": "ab"}{"text": "abc"}', D,
     [{"text": "a"}, {"text": "ab"}, {"text": "abc"}],
     "concatenated progressive snapshots become an array", JM),
    ("numbered-list-json", "The top items are:\n\n1. first\n2. second\n\n"
     '{"items": ["first", "second"]}', D,
     {"items": ["first", "second"]},
     "numbered prose lines before the value are junk", JM),
    ("bool-yes", '{"enabled": yes}', A,
     [{"enabled": "yes"}, {"enabled": True}],
     "yes/no are not JSON booleans: string (safe) or boolean (YAML-ish)",
     JM),
    ("env-style", '{"DEBUG": true, "PORT": 8080, "HOST": localhost}', D,
     {"DEBUG": True, "PORT": 8080, "HOST": "localhost"},
     "bare hostname value becomes a string", JM),
    ("crlf", '{"a": 1,\r\n"b": 2}', D, {"a": 1, "b": 2},
     "CRLF line endings are whitespace", JM),
    ("cr-only", '{"a": 1,\r"b": 2}', D, {"a": 1, "b": 2},
     "lone CR is whitespace", JM),
    ("hex-number", '{"color": 0xFF}', A,
     [{"color": "0xFF"}, {"color": 255}],
     "hex is not JSON: string (safe) or decoded int", JM),
]

HAND["unrecoverable"] = [
    ("empty", "", U, None, "nothing to mend", JJ),
    ("ws-only", "   \n\t  ", U, None, "nothing to mend", JM),
    ("backticks-only", "```", U, None, "an empty fence has no payload", JM),
    ("commas-only", ",,,", U, None, "separators alone carry no value", JM),
    ("closers-only", "}}]]", U, None, "closers alone carry no value", JM),
]


# ---------------------------------------------------------------------------
# Programmatic: truncation sweep over realistic documents
# ---------------------------------------------------------------------------

TOOLCALL = ('{"id": "call_8x2K", "type": "function", "function": '
            '{"name": "get_weather", "arguments": "{\\"city\\": '
            '\\"San Francisco\\", \\"unit\\": \\"celsius\\"}"}, '
            '"list": [1, 2.5, true, false, null], "note": "emoji 😀 ok"}')

AGENT_PLAN = ('{"plan": [{"step": 1, "action": "search", "query": '
              '"weather 北京"}, {"step": 2, "action": "summarize", '
              '"max_words": 120}], "confidence": 0.92, "fallback": null, '
              '"tags": ["fast", "cheap"]}')


def _close_truncated(text):
    """Reference truncation-closing semantics for *clean* JSON prefixes.

    This independent mini-parser defines the deterministic expectation for
    the truncation sweep: tokenize the valid-JSON prefix, then at EOF
    complete the partial token (string content kept, literal prefix
    completed, number completed) and close all open containers, pairing a
    dangling key with null.
    """
    i, n = 0, len(text)
    stack = []   # ('o', dict, key) | ('a', list)
    top = []

    def attach(v):
        if not stack:
            top.append(v)
        elif stack[-1][0] == "a":
            stack[-1][1].append(v)
        else:
            kind, container, key = stack[-1]
            if key is None:
                stack[-1] = (kind, container, v if isinstance(v, str)
                             else json.dumps(v))
            else:
                container[key] = v
                stack[-1] = (kind, container, None)

    def in_object_expecting_key():
        return stack and stack[-1][0] == "o" and stack[-1][2] is None

    while i < n:
        c = text[i]
        if c in " \t\n\r":
            i += 1
            continue
        if c == "{":
            stack.append(("o", {}, None))
            i += 1
        elif c == "[":
            stack.append(("a", []))
            i += 1
        elif c in "}]":
            fr = stack.pop()
            if fr[0] == "o":
                if fr[2] is not None:
                    fr[1][fr[2]] = None
                attach(fr[1])
            else:
                attach(fr[1])
            i += 1
        elif c in ",:":
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            closed = False
            while j < n:
                ch = text[j]
                if ch == "\\":
                    if j + 1 >= n:
                        break  # dangling backslash at EOF
                    esc = text[j + 1]
                    if esc == "u":
                        if j + 6 > n:
                            j = n
                            break
                        buf.append(chr(int(text[j + 2:j + 6], 16)))
                        j += 6
                        continue
                    buf.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b",
                                "f": "\f"}.get(esc, esc))
                    j += 2
                    continue
                if ch == '"':
                    closed = True
                    break
                buf.append(ch)
                j += 1
            raw = "".join(buf)
            if not closed:
                # surrogate halves from a cut 😀 pair
                raw = "".join(ch for ch in raw
                              if not 0xD800 <= ord(ch) <= 0xDFFF)
            attach(raw)
            i = j + 1 if closed else n
        else:
            j = i
            while j < n and text[j] not in ' \t\n\r,:]}"':
                j += 1
            tok = text[i:j]
            i = j
            for lit, val in (("true", True), ("false", False), ("null", None)):
                if lit.startswith(tok) and tok:
                    attach(val)
                    break
            else:
                t = tok.rstrip(".eE+-")
                if not t or t in "+-":
                    continue
                if "." in t or "e" in t or "E" in t:
                    attach(float(t))
                else:
                    attach(int(t))

    while stack:
        fr = stack.pop()
        if fr[0] == "o":
            if fr[2] is not None:
                fr[1][fr[2]] = None
            v = fr[1]
        else:
            v = fr[1]
        attach(v)
    if not top:
        return None, False
    return (top[0] if len(top) == 1 else top), True


def truncation_sweep():
    cases = []
    for label, doc, step in (("toolcall", TOOLCALL, 4),
                             ("plan", AGENT_PLAN, 5)):
        for cut in range(2, len(doc), step):
            frag = doc[:cut]
            expected, ok = _close_truncated(frag)
            if not ok:
                continue
            cases.append((
                "sweep-%s-%03d" % (label, cut), frag, D, expected,
                "prefix of a valid document cut at offset %d; expectation "
                "from the reference truncation-closing rules" % cut, JM))
    return cases


# ---------------------------------------------------------------------------
# Programmatic: seeded degradations with a known ground truth
# ---------------------------------------------------------------------------

DOCS = {
    "config": {"server": {"host": "api.example.com", "port": 8443,
                          "tls": True},
               "retries": 3, "timeout": 2.5, "tags": ["prod", "eu-west"],
               "fallback": None},
    "report": {"title": "Q3 sales", "rows": [
        {"region": "EMEA", "value": 1204.5, "ok": True},
        {"region": "APAC", "value": 980, "ok": False}],
        "total": 2184.5},
    "chat": {"role": "assistant", "content": "Sure thing", "steps": [1, 2, 3],
             "done": True, "score": 0.87, "extra": None},
    "i18n": {"city": "北京", "name": "café", "emoji": "🎉",
             "langs": ["中文", "Français", "Русский"], "count": 3},
}


def _emit(value, *, quote='"', quote_keys=True, trailing_comma=False,
          py_literals=False, comments=False, newline_commas=False,
          indent=0):
    """Serialize `value` with deliberate damage applied."""
    pad = " " * indent
    if isinstance(value, dict):
        parts = []
        items = list(value.items())
        for idx, (k, v) in enumerate(items):
            key = (quote + k + quote) if quote_keys else k
            body = _emit(v, quote=quote, quote_keys=quote_keys,
                         trailing_comma=trailing_comma,
                         py_literals=py_literals, comments=comments,
                         newline_commas=newline_commas, indent=indent + 2)
            comment = " // item" if comments and idx == 0 else ""
            parts.append("%s  %s: %s" % (pad, key, body) + comment)
        sep = "\n" if newline_commas else ",\n"
        tail = "," if trailing_comma and parts else ""
        return "{\n" + sep.join(parts) + tail + "\n" + pad + "}"
    if isinstance(value, list):
        body = ", ".join(
            _emit(v, quote=quote, quote_keys=quote_keys,
                  trailing_comma=False, py_literals=py_literals,
                  indent=indent) for v in value)
        tail = "," if trailing_comma and value else ""
        return "[" + body + tail + "]"
    if value is True:
        return "True" if py_literals else "true"
    if value is False:
        return "False" if py_literals else "false"
    if value is None:
        return "None" if py_literals else "null"
    if isinstance(value, str):
        return quote + value + quote
    return json.dumps(value)


def degradations():
    cases = []
    combos = [
        ("single-quotes", dict(quote="'")),
        ("bare-keys", dict(quote_keys=False)),
        ("trailing-commas", dict(trailing_comma=True)),
        ("python-literals", dict(py_literals=True)),
        ("comments", dict(comments=True)),
        ("newline-commas", dict(newline_commas=True)),
        ("single-bare", dict(quote="'", quote_keys=False)),
        ("python-trailing", dict(py_literals=True, trailing_comma=True)),
        ("kitchen-sink", dict(quote="'", quote_keys=False,
                              trailing_comma=True, py_literals=True,
                              comments=True)),
    ]
    for doc_name, doc in DOCS.items():
        for combo_name, kw in combos:
            text = _emit(doc, **kw)
            cases.append((
                "degrade-%s-%s" % (doc_name, combo_name), text, D, doc,
                "mechanically degraded (%s) from a known document; the "
                "original value is the only correct repair" % combo_name,
                JM))
            # fenced variant
            cases.append((
                "degrade-%s-%s-fenced" % (doc_name, combo_name),
                "Here is the result:\n```json\n" + text + "\n```\n", D, doc,
                "same degradation wrapped in prose + markdown fence",
                JM))
    return cases


# ---------------------------------------------------------------------------
# Adversarial (all checked for validity / no crash, not exact value)
# ---------------------------------------------------------------------------

def adversarial():
    deep = "[" * 600 + "1" + "]" * 600
    deep_unclosed = "[" * 600 + "1"
    deep_obj = '{"a":' * 300 + "1" + "}" * 300
    quote_storm = '{"a": "' + 'x " y ' * 400 + '"}'
    long_string = '{"text": "' + ("lorem ipsum " * 2000) + '"}'
    return [
        ("deep-array", deep, D, None,
         "600 levels of nesting must not crash or recurse", JM, "valid"),
        ("deep-array-unclosed", deep_unclosed, D, None,
         "600 unclosed levels must close cleanly", JM, "valid"),
        ("deep-object", deep_obj, D, None,
         "600 alternating object levels", JM, "valid"),
        ("quote-storm", quote_storm, D, None,
         "hundreds of candidate close quotes must not blow up "
         "(bounded backtracking)", JM, "valid"),
        ("long-clean-string", long_string, D,
         {"text": ("lorem ipsum " * 2000)},
         "a 24KB clean string is one slice", JM),
    ]


# ---------------------------------------------------------------------------


def main():
    if os.path.isdir(CASES):
        shutil.rmtree(CASES)
    count = 0
    all_groups = dict(HAND)
    all_groups["truncation-sweep"] = truncation_sweep()
    all_groups["degraded"] = degradations()
    all_groups["adversarial"] = adversarial()

    names = set()
    for category, entries in all_groups.items():
        cat_dir = os.path.join(CASES, category)
        os.makedirs(cat_dir, exist_ok=True)
        for entry in entries:
            if len(entry) == 6:
                name, text, verdict, payload, rationale, source = entry
                check = "value"
            else:
                name, text, verdict, payload, rationale, source, check = entry
            assert name not in names, "duplicate case name: " + name
            names.add(name)
            case = {"input": text, "verdict": verdict,
                    "rationale": rationale, "source": source}
            if check != "value":
                case["check"] = check
            if verdict == D and check == "value":
                case["expected"] = payload
            elif verdict == A:
                case["accepted"] = payload
            path = os.path.join(cat_dir, name + ".json")
            try:
                blob = json.dumps(case, ensure_ascii=False, indent=2)
                blob.encode("utf-8")
            except UnicodeEncodeError:
                # lone surrogates in accepted values: keep them escaped
                blob = json.dumps(case, ensure_ascii=True, indent=2)
            with open(path, "w", encoding="utf-8") as f:
                f.write(blob)
                f.write("\n")
            count += 1
    print("wrote %d cases" % count)


if __name__ == "__main__":
    sys.exit(main())
