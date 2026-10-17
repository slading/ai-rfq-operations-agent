# F3-A1 — QA Coverage Matrix, revision 4 (89 gate rows + 219 regression rows)

Gate: `docs/reviews/f3a/test_redaction_adversarial.py` at commit `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`.

Regression suite: `f3a1-regression-r4.py` (219 tests), adopted as
`tests/unit/test_redaction_regression.py` by the implementation PR.

Prototype: `f3a1-prototype-r4.py`. Baseline `d5611eef`; revision 3 `bee3105`.

Commands:

```
PYTHONPATH=src:<dir> .venv/bin/python -m pytest test_redaction_adversarial.py -q   # 89 passed
PYTHONPATH=src:<dir> .venv/bin/python -m pytest f3a1-regression-r4.py -q           # 219 passed
```

**Totals: 89/89 gate, 219/219 regression, 0 failed.** Rows marked ⟦R4⟧ are
new or changed in revision 4; the rest are revision 3 and re-verified.

## A. Gate — the 89 QA tests

| # | QA test | Finding | Mechanism that makes it pass | Result |
| --- | --- | --- | --- | --- |
| 1 | `TestSummarizePayloadSerializationPaths::test_truncated_mapping_never_leaks_sensitive_value[api_key]` | B-01 / C-1 | structural redaction happens before serialisation; truncation cuts the redacted bytes | PASS |
| 2 | `TestSummarizePayloadSerializationPaths::test_truncated_mapping_never_leaks_sensitive_value[token]` | B-01 / C-1 | structural redaction happens before serialisation; truncation cuts the redacted bytes | PASS |
| 3 | `TestSummarizePayloadSerializationPaths::test_truncated_mapping_never_leaks_sensitive_value[password]` | B-01 / C-1 | structural redaction happens before serialisation; truncation cuts the redacted bytes | PASS |
| 4 | `TestSummarizePayloadSerializationPaths::test_truncated_mapping_never_leaks_sensitive_value[secret]` | B-01 / C-1 | structural redaction happens before serialisation; truncation cuts the redacted bytes | PASS |
| 5 | `TestSummarizePayloadSerializationPaths::test_truncated_mapping_never_leaks_sensitive_value[authorization]` | B-01 / C-1 | structural redaction happens before serialisation; truncation cuts the redacted bytes | PASS |
| 6 | `TestSummarizePayloadSerializationPaths::test_truncated_never_leaks_a_secret_prefix` | B-01 / C-1 | structural redaction happens before serialisation; truncation cuts the redacted bytes | PASS |
| 7 | `TestSummarizePayloadSerializationPaths::test_list_payload_is_structurally_redacted` | B-01 / C-1 | structural redaction happens before serialisation; truncation cuts the redacted bytes | PASS |
| 8 | `TestSummarizePayloadSerializationPaths::test_truncated_email_is_removed` | B-01 / C-1 | structural redaction happens before serialisation; truncation cuts the redacted bytes | PASS |
| 9 | `TestQuotedKeySyntaxes::test_json_string_with_sensitive_key[api_key]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 10 | `TestQuotedKeySyntaxes::test_json_string_with_sensitive_key[token]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 11 | `TestQuotedKeySyntaxes::test_json_string_with_sensitive_key[password]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 12 | `TestQuotedKeySyntaxes::test_json_string_with_sensitive_key[secret]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 13 | `TestQuotedKeySyntaxes::test_json_string_with_sensitive_key[authorization]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 14 | `TestQuotedKeySyntaxes::test_python_repr_with_sensitive_key[api_key]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 15 | `TestQuotedKeySyntaxes::test_python_repr_with_sensitive_key[token]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 16 | `TestQuotedKeySyntaxes::test_python_repr_with_sensitive_key[password]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 17 | `TestQuotedKeySyntaxes::test_python_repr_with_sensitive_key[secret]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 18 | `TestQuotedKeySyntaxes::test_python_repr_with_sensitive_key[authorization]` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 19 | `TestQuotedKeySyntaxes::test_json_embedded_in_an_innocuous_field` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 20 | `TestQuotedKeySyntaxes::test_quoted_dotenv_value` | B-02 / C-1 | key+separator scanner accepts quoted keys and quoted values; JSON/literal re-parse for nested strings | PASS |
| 21 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[x-api-key]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 22 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[X-API-KEY]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 23 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[proxy-authorization]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 24 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[cookie]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 25 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[set-cookie]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 26 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[secret_key]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 27 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[private_key]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 28 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[aws_secret_access_key]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 29 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[db_password]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 30 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[api key]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 31 | `TestSensitiveKeyNormalisation::test_header_style_and_suffixed_keys_are_wholesale_redacted[api_key:]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 32 | `TestSensitiveKeyNormalisation::test_non_string_keys_do_not_crash_the_choke_point[1]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 33 | `TestSensitiveKeyNormalisation::test_non_string_keys_do_not_crash_the_choke_point[None]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 34 | `TestSensitiveKeyNormalisation::test_non_string_keys_do_not_crash_the_choke_point[2.5]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 35 | `TestSensitiveKeyNormalisation::test_non_string_keys_do_not_crash_the_choke_point[key3]` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 36 | `TestSensitiveKeyNormalisation::test_secret_or_email_used_as_a_key_is_redacted` | B-03 / C-2, C-4 | normalised key vocabulary (exact + suffix); non-str key coercion; secret-in-key renaming | PASS |
| 37 | `TestPairArrays::test_list_of_pairs_is_treated_like_a_mapping` | B-04 / C-4 | pair arrays, name/value records, set recursion, bytes decoding | PASS |
| 38 | `TestPairArrays::test_name_value_dicts` | B-04 / C-4 | pair arrays, name/value records, set recursion, bytes decoding | PASS |
| 39 | `TestPairArrays::test_sets_are_recursed[set]` | B-04 / C-4 | pair arrays, name/value records, set recursion, bytes decoding | PASS |
| 40 | `TestPairArrays::test_sets_are_recursed[frozenset]` | B-04 / C-4 | pair arrays, name/value records, set recursion, bytes decoding | PASS |
| 41 | `TestPairArrays::test_bytes_are_not_passed_through_verbatim[bytes]` | B-04 / C-4 | pair arrays, name/value records, set recursion, bytes decoding | PASS |
| 42 | `TestPairArrays::test_bytes_are_not_passed_through_verbatim[bytearray]` | B-04 / C-4 | pair arrays, name/value records, set recursion, bytes decoding | PASS |
| 43 | `TestValueShapes::test_short_values_under_an_explicit_secret_key_are_redacted[password=hunter2]` | B-05 / C-2 | strong-key tier redacts any non-empty value; PEM block rule; comma separator | PASS |
| 44 | `TestValueShapes::test_short_values_under_an_explicit_secret_key_are_redacted[password: hunter2]` | B-05 / C-2 | strong-key tier redacts any non-empty value; PEM block rule; comma separator | PASS |
| 45 | `TestValueShapes::test_short_values_under_an_explicit_secret_key_are_redacted[passwd=hunter2]` | B-05 / C-2 | strong-key tier redacts any non-empty value; PEM block rule; comma separator | PASS |
| 46 | `TestValueShapes::test_short_values_under_an_explicit_secret_key_are_redacted[api_key=abc12345]` | B-05 / C-2 | strong-key tier redacts any non-empty value; PEM block rule; comma separator | PASS |
| 47 | `TestValueShapes::test_short_values_under_an_explicit_secret_key_are_redacted[Bearer hunter2]` | B-05 / C-2 | strong-key tier redacts any non-empty value; PEM block rule; comma separator | PASS |
| 48 | `TestValueShapes::test_basic_auth_scheme` | B-05 / C-2 | strong-key tier redacts any non-empty value; PEM block rule; comma separator | PASS |
| 49 | `TestValueShapes::test_pem_block` | B-05 / C-2 | strong-key tier redacts any non-empty value; PEM block rule; comma separator | PASS |
| 50 | `TestValueShapes::test_csv_separator` | B-05 / C-2 | strong-key tier redacts any non-empty value; PEM block rule; comma separator | PASS |
| 51 | `TestUrlCredentials::test_userinfo_password_is_removed[postgresql://rfq:gsk_FAKEsupersecret0123456789@db:5432/rfq]` | B-06 / C-1 | URL userinfo rule runs before e-mail; weak `key` in query strings | PASS |
| 52 | `TestUrlCredentials::test_userinfo_password_is_removed[https://user:gsk_FAKEsupersecret0123456789@host/path]` | B-06 / C-1 | URL userinfo rule runs before e-mail; weak `key` in query strings | PASS |
| 53 | `TestUrlCredentials::test_userinfo_password_is_removed[redis://:gsk_FAKEsupersecret0123456789@cache:6379/0]` | B-06 / C-1 | URL userinfo rule runs before e-mail; weak `key` in query strings | PASS |
| 54 | `TestUrlCredentials::test_userinfo_password_is_removed[amqp://guest:gsk_FAKEsupersecret0123456789@mq]` | B-06 / C-1 | URL userinfo rule runs before e-mail; weak `key` in query strings | PASS |
| 55 | `TestUrlCredentials::test_userinfo_password_short` | B-06 / C-1 | URL userinfo rule runs before e-mail; weak `key` in query strings | PASS |
| 56 | `TestUrlCredentials::test_query_string_key_param_short_value` | B-06 / C-1 | URL userinfo rule runs before e-mail; weak `key` in query strings | PASS |
| 57 | `TestUnicode::test_fullwidth_punctuation_and_letters[api_key\uff1dgsk_FAKEsupersecret0123456789]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 58 | `TestUnicode::test_fullwidth_punctuation_and_letters[api_key\uff1agsk_FAKEsupersecret0123456789]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 59 | `TestUnicode::test_fullwidth_punctuation_and_letters[\uff41\uff50\uff49_\uff4b\uff45\uff59=gsk_FAKEsupersecret0123456789]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 60 | `TestUnicode::test_internationalised_and_quoted_emails[kontakt@przyk\u0142ad.pl]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 61 | `TestUnicode::test_internationalised_and_quoted_emails[\u017c\xf3\u0142w@example.com]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 62 | `TestUnicode::test_internationalised_and_quoted_emails["john doe"@example.com]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 63 | `TestUnicode::test_email_followed_by_word_char_is_still_redacted[_]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 64 | `TestUnicode::test_email_followed_by_word_char_is_still_redacted[\u017c]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 65 | `TestUnicode::test_email_followed_by_word_char_is_still_redacted[0]` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 66 | `TestUnicode::test_lone_surrogate_does_not_crash_summarize` | B-07 / C-3 | NFKC normalisation, Unicode e-mail detector, surrogate replacement | PASS |
| 67 | `TestTruncationBoundary::test_stored_truncated_copy_respects_max_bytes[\u017c]` | B-08 / C-8 | byte-accurate cap; `max_bytes` must be positive | PASS |
| 68 | `TestTruncationBoundary::test_stored_truncated_copy_respects_max_bytes[\U0001f600]` | B-08 / C-8 | byte-accurate cap; `max_bytes` must be positive | PASS |
| 69 | `TestTruncationBoundary::test_max_bytes_must_be_positive` | B-08 / C-8 | byte-accurate cap; `max_bytes` must be positive | PASS |
| 70 | `TestRegexPerformance::test_48kb_hostile_input_redacts_in_under_200ms[key-run]` | B-09 / C-1 | bounded quantifiers, hint gate, token-based e-mail detector | PASS |
| 71 | `TestRegexPerformance::test_48kb_hostile_input_redacts_in_under_200ms[dash-key]` | B-09 / C-1 | bounded quantifiers, hint gate, token-based e-mail detector | PASS |
| 72 | `TestRegexPerformance::test_48kb_hostile_input_redacts_in_under_200ms[email-dots]` | B-09 / C-1 | bounded quantifiers, hint gate, token-based e-mail detector | PASS |
| 73 | `TestRegexPerformance::test_48kb_hostile_input_redacts_in_under_200ms[email-digits]` | B-09 / C-1 | bounded quantifiers, hint gate, token-based e-mail detector | PASS |
| 74 | `TestRegexPerformance::test_scaling_is_roughly_linear` | B-09 / C-1 | bounded quantifiers, hint gate, token-based e-mail detector | PASS |
| 75 | `TestExceptionRendering::test_validation_error_text_through_redact_text` | B-10 / C-6 | provider-token backstop + `input`/`input_value` structured error fields | PASS |
| 76 | `TestExceptionRendering::test_validation_error_errors_through_redact_mapping` | B-10 / C-6 | provider-token backstop + `input`/`input_value` structured error fields | PASS |
| 77 | `TestFalsePositives::test_business_identifiers_survive[idempotency_key=ORD-2026-000123]` | B-11 / C-5 | bounded values, business-ID allowlist, vocabulary that ignores benign substrings | PASS |
| 78 | `TestFalsePositives::test_business_identifiers_survive[primary_key=RFQ-2026-000042]` | B-11 / C-5 | bounded values, business-ID allowlist, vocabulary that ignores benign substrings | PASS |
| 79 | `TestFalsePositives::test_business_identifiers_survive[Keyboard=KB-2026-BLACK-US]` | B-11 / C-5 | bounded values, business-ID allowlist, vocabulary that ignores benign substrings | PASS |
| 80 | `TestFalsePositives::test_business_identifiers_survive[Turkey: ANKARA-WAREHOUSE-01]` | B-11 / C-5 | bounded values, business-ID allowlist, vocabulary that ignores benign substrings | PASS |
| 81 | `TestFalsePositives::test_business_identifiers_survive[token_count: 1234567890123]` | B-11 / C-5 | bounded values, business-ID allowlist, vocabulary that ignores benign substrings | PASS |
| 82 | `TestFalsePositives::test_business_identifiers_survive[Bearer Instrument-2026-A]` | B-11 / C-5 | bounded values, business-ID allowlist, vocabulary that ignores benign substrings | PASS |
| 83 | `TestFalsePositives::test_header_rule_does_not_eat_the_rest_of_the_line` | B-11 / C-5 | bounded values, business-ID allowlist, vocabulary that ignores benign substrings | PASS |
| 84 | `TestDeterministicFuzz::test_key_sep_secret_is_always_removed[0]` | B-02 / I1, I2 | separator/wrapper-tolerant scanner; idempotent by construction | PASS |
| 85 | `TestDeterministicFuzz::test_key_sep_secret_is_always_removed[1]` | B-02 / I1, I2 | separator/wrapper-tolerant scanner; idempotent by construction | PASS |
| 86 | `TestDeterministicFuzz::test_key_sep_secret_is_always_removed[2]` | B-02 / I1, I2 | separator/wrapper-tolerant scanner; idempotent by construction | PASS |
| 87 | `TestDeterministicFuzz::test_key_sep_secret_is_always_removed[3]` | B-02 / I1, I2 | separator/wrapper-tolerant scanner; idempotent by construction | PASS |
| 88 | `TestDeterministicFuzz::test_redaction_is_idempotent[0]` | B-02 / I1, I2 | separator/wrapper-tolerant scanner; idempotent by construction | PASS |
| 89 | `TestDeterministicFuzz::test_redaction_is_idempotent[1]` | B-02 / I1, I2 | separator/wrapper-tolerant scanner; idempotent by construction | PASS |

## B. Regression suite — the 219 tests

The 57 new tests cover every shape the revision-3 prototype handled wrongly
**or must not lose** while fixing it; 37 of them fail on that prototype
(22 container, 15 escape-boundary) and 20 pass, which is the falsifiability
and the preservation half. The 162 inherited tests keep the C-8/C-9/C-10/C-12
guarantees.

| # | Regression test | Blocker | Mechanism that makes it pass | Result |
| --- | --- | --- | --- | --- |
| 1 | `TestPemLinearScanner::test_48kb_of_begin_markers_is_fast` | C-8 | C-8: `_redact_pem` marker scan, O(n) — 0.57 ms; budget 0.2 s | PASS |
| 2 | `TestPemLinearScanner::test_1mb_of_begin_markers_is_fast` | C-8 | C-8: same scanner at 1 MB — 12.03 ms; budget 0.5 s | PASS |
| 3 | `TestPemLinearScanner::test_repeated_begin_and_end_pairs_are_fast` | C-8 | C-8: pairing BEGIN with the next END is one cursor walk | PASS |
| 4 | `TestPemLinearScanner::test_real_pem_block_is_removed` | C-8 | C-8: a genuine PEM block becomes `[REDACTED]` | PASS |
| 5 | `TestPemLinearScanner::test_unterminated_block_fails_closed` | C-8 | C-8: `BEGIN` without `END` withholds to end of input | PASS |
| 6 | `TestPemLinearScanner::test_markers_without_a_body_are_still_bounded` | C-8 | C-8: marker-heavy text with no body stays under budget | PASS |
| 7 | `TestPemLinearScanner::test_summarize_path_48kb` | C-8 | C-8: `summarize_payload` runs the same scanner — 0.83 ms | PASS |
| 8 | `TestPemLinearScanner::test_summarize_path_1mb` | C-8 | C-8: 1 MB through the summary path — 16.53 ms | PASS |
| 9 | `TestPemLinearScanner::test_summarize_path_preserves_hash_and_status` | C-8 | C-8 fail-closed still returns a digest and a closed status | PASS |
| 10 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[accesskey]` | C-9 | C-9: derived text tier; `hunter2` under `accesskey` redacted | PASS |
| 11 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[accesskeyid]` | C-9 | C-9: derived text tier; `hunter2` under `accesskeyid` redacted | PASS |
| 12 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[accesstoken]` | C-9 | C-9: derived text tier; `hunter2` under `accesstoken` redacted | PASS |
| 13 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[apikey]` | C-9 | C-9: derived text tier; `hunter2` under `apikey` redacted | PASS |
| 14 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[apikeyid]` | C-9 | C-9: derived text tier; `hunter2` under `apikeyid` redacted | PASS |
| 15 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[apikeys]` | C-9 | C-9: derived text tier; `hunter2` under `apikeys` redacted | PASS |
| 16 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[auth]` | C-9 | C-9: derived text tier; `hunter2` under `auth` redacted | PASS |
| 17 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[authentication]` | C-9 | C-9: derived text tier; `hunter2` under `authentication` redacted | PASS |
| 18 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[authorization]` | C-9 | C-9: derived text tier; `hunter2` under `authorization` redacted | PASS |
| 19 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[authorizationcode]` | C-9 | C-9: derived text tier; `hunter2` under `authorizationcode` redacted | PASS |
| 20 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[awssecretaccesskey]` | C-9 | C-9: derived text tier; `hunter2` under `awssecretaccesskey` redacted | PASS |
| 21 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[bearer]` | C-9 | C-9: derived text tier; `hunter2` under `bearer` redacted | PASS |
| 22 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[clientsecret]` | C-9 | C-9: derived text tier; `hunter2` under `clientsecret` redacted | PASS |
| 23 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[cookie]` | C-9 | C-9: derived text tier; `hunter2` under `cookie` redacted | PASS |
| 24 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[credential]` | C-9 | C-9: derived text tier; `hunter2` under `credential` redacted | PASS |
| 25 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[credentials]` | C-9 | C-9: derived text tier; `hunter2` under `credentials` redacted | PASS |
| 26 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[customeremail]` | C-9 | C-9: derived text tier; `hunter2` under `customeremail` redacted | PASS |
| 27 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[email]` | C-9 | C-9: derived text tier; `hunter2` under `email` redacted | PASS |
| 28 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[emailaddress]` | C-9 | C-9: derived text tier; `hunter2` under `emailaddress` redacted | PASS |
| 29 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[encryptionkey]` | C-9 | C-9: derived text tier; `hunter2` under `encryptionkey` redacted | PASS |
| 30 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[groqapikey]` | C-9 | C-9: derived text tier; `hunter2` under `groqapikey` redacted | PASS |
| 31 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[input]` | C-9 | C-9: derived text tier; `hunter2` under `input` redacted | PASS |
| 32 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[inputvalue]` | C-9 | C-9: derived text tier; `hunter2` under `inputvalue` redacted | PASS |
| 33 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[jwt]` | C-9 | C-9: derived text tier; `hunter2` under `jwt` redacted | PASS |
| 34 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[key]` | C-9 | C-9: derived text tier; `hunter2` under `key` redacted | PASS |
| 35 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[oauth]` | C-9 | C-9: derived text tier; `hunter2` under `oauth` redacted | PASS |
| 36 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[operatortoken]` | C-9 | C-9: derived text tier; `hunter2` under `operatortoken` redacted | PASS |
| 37 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[passphrase]` | C-9 | C-9: derived text tier; `hunter2` under `passphrase` redacted | PASS |
| 38 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[passwd]` | C-9 | C-9: derived text tier; `hunter2` under `passwd` redacted | PASS |
| 39 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[password]` | C-9 | C-9: derived text tier; `hunter2` under `password` redacted | PASS |
| 40 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[passwords]` | C-9 | C-9: derived text tier; `hunter2` under `passwords` redacted | PASS |
| 41 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[privatekey]` | C-9 | C-9: derived text tier; `hunter2` under `privatekey` redacted | PASS |
| 42 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[proxyauthorization]` | C-9 | C-9: derived text tier; `hunter2` under `proxyauthorization` redacted | PASS |
| 43 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[refreshtoken]` | C-9 | C-9: derived text tier; `hunter2` under `refreshtoken` redacted | PASS |
| 44 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[secret]` | C-9 | C-9: derived text tier; `hunter2` under `secret` redacted | PASS |
| 45 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[secretaccesskey]` | C-9 | C-9: derived text tier; `hunter2` under `secretaccesskey` redacted | PASS |
| 46 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[secretkey]` | C-9 | C-9: derived text tier; `hunter2` under `secretkey` redacted | PASS |
| 47 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[secrets]` | C-9 | C-9: derived text tier; `hunter2` under `secrets` redacted | PASS |
| 48 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[senderemail]` | C-9 | C-9: derived text tier; `hunter2` under `senderemail` redacted | PASS |
| 49 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[sessionid]` | C-9 | C-9: derived text tier; `hunter2` under `sessionid` redacted | PASS |
| 50 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[sessiontoken]` | C-9 | C-9: derived text tier; `hunter2` under `sessiontoken` redacted | PASS |
| 51 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[setcookie]` | C-9 | C-9: derived text tier; `hunter2` under `setcookie` redacted | PASS |
| 52 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[signingkey]` | C-9 | C-9: derived text tier; `hunter2` under `signingkey` redacted | PASS |
| 53 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[token]` | C-9 | C-9: derived text tier; `hunter2` under `token` redacted | PASS |
| 54 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[tokens]` | C-9 | C-9: derived text tier; `hunter2` under `tokens` redacted | PASS |
| 55 | `TestTextKeyPolicy::test_every_exact_key_redacts_a_non_provider_secret[useremail]` | C-9 | C-9: derived text tier; `hunter2` under `useremail` redacted | PASS |
| 56 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[accesskey]` | C-9 | C-9: structural control for `accesskey` | PASS |
| 57 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[accesskeyid]` | C-9 | C-9: structural control for `accesskeyid` | PASS |
| 58 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[accesstoken]` | C-9 | C-9: structural control for `accesstoken` | PASS |
| 59 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[apikey]` | C-9 | C-9: structural control for `apikey` | PASS |
| 60 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[apikeyid]` | C-9 | C-9: structural control for `apikeyid` | PASS |
| 61 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[apikeys]` | C-9 | C-9: structural control for `apikeys` | PASS |
| 62 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[auth]` | C-9 | C-9: structural control for `auth` | PASS |
| 63 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[authentication]` | C-9 | C-9: structural control for `authentication` | PASS |
| 64 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[authorization]` | C-9 | C-9: structural control for `authorization` | PASS |
| 65 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[authorizationcode]` | C-9 | C-9: structural control for `authorizationcode` | PASS |
| 66 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[awssecretaccesskey]` | C-9 | C-9: structural control for `awssecretaccesskey` | PASS |
| 67 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[bearer]` | C-9 | C-9: structural control for `bearer` | PASS |
| 68 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[clientsecret]` | C-9 | C-9: structural control for `clientsecret` | PASS |
| 69 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[cookie]` | C-9 | C-9: structural control for `cookie` | PASS |
| 70 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[credential]` | C-9 | C-9: structural control for `credential` | PASS |
| 71 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[credentials]` | C-9 | C-9: structural control for `credentials` | PASS |
| 72 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[customeremail]` | C-9 | C-9: structural control for `customeremail` | PASS |
| 73 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[email]` | C-9 | C-9: structural control for `email` | PASS |
| 74 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[emailaddress]` | C-9 | C-9: structural control for `emailaddress` | PASS |
| 75 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[encryptionkey]` | C-9 | C-9: structural control for `encryptionkey` | PASS |
| 76 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[groqapikey]` | C-9 | C-9: structural control for `groqapikey` | PASS |
| 77 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[input]` | C-9 | C-9: structural control for `input` | PASS |
| 78 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[inputvalue]` | C-9 | C-9: structural control for `inputvalue` | PASS |
| 79 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[jwt]` | C-9 | C-9: structural control for `jwt` | PASS |
| 80 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[key]` | C-9 | C-9: structural control for `key` | PASS |
| 81 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[oauth]` | C-9 | C-9: structural control for `oauth` | PASS |
| 82 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[operatortoken]` | C-9 | C-9: structural control for `operatortoken` | PASS |
| 83 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[passphrase]` | C-9 | C-9: structural control for `passphrase` | PASS |
| 84 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[passwd]` | C-9 | C-9: structural control for `passwd` | PASS |
| 85 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[password]` | C-9 | C-9: structural control for `password` | PASS |
| 86 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[passwords]` | C-9 | C-9: structural control for `passwords` | PASS |
| 87 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[privatekey]` | C-9 | C-9: structural control for `privatekey` | PASS |
| 88 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[proxyauthorization]` | C-9 | C-9: structural control for `proxyauthorization` | PASS |
| 89 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[refreshtoken]` | C-9 | C-9: structural control for `refreshtoken` | PASS |
| 90 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[secret]` | C-9 | C-9: structural control for `secret` | PASS |
| 91 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[secretaccesskey]` | C-9 | C-9: structural control for `secretaccesskey` | PASS |
| 92 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[secretkey]` | C-9 | C-9: structural control for `secretkey` | PASS |
| 93 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[secrets]` | C-9 | C-9: structural control for `secrets` | PASS |
| 94 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[senderemail]` | C-9 | C-9: structural control for `senderemail` | PASS |
| 95 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[sessionid]` | C-9 | C-9: structural control for `sessionid` | PASS |
| 96 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[sessiontoken]` | C-9 | C-9: structural control for `sessiontoken` | PASS |
| 97 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[setcookie]` | C-9 | C-9: structural control for `setcookie` | PASS |
| 98 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[signingkey]` | C-9 | C-9: structural control for `signingkey` | PASS |
| 99 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[token]` | C-9 | C-9: structural control for `token` | PASS |
| 100 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[tokens]` | C-9 | C-9: structural control for `tokens` | PASS |
| 101 | `TestTextKeyPolicy::test_every_exact_key_redacts_structurally[useremail]` | C-9 | C-9: structural control for `useremail` | PASS |
| 102 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[x-api-key]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 103 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[X-API-KEY]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 104 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[db_password]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 105 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[operator-token]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 106 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[accessToken]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 107 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[clientSecret]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 108 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[senderEmail]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 109 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[customer_email]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 110 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[aws_secret_access_key]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 111 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[proxy-authorization]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 112 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[set-cookie]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 113 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[session_id]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 114 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[api` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 115 | `TestTextKeyPolicy::test_suffixed_and_header_spellings_redact[db.passwd]` | C-9 | C-9/C-4: suffix/header spelling reaches the derived tier | PASS |
| 116 | `TestTextKeyPolicy::test_keys_named_in_c_minus_9[passphrase]` | C-9 | C-9: the keys Session D named as leaking in revision 2 | PASS |
| 117 | `TestTextKeyPolicy::test_keys_named_in_c_minus_9[auth]` | C-9 | C-9: the keys Session D named as leaking in revision 2 | PASS |
| 118 | `TestTextKeyPolicy::test_keys_named_in_c_minus_9[authentication]` | C-9 | C-9: the keys Session D named as leaking in revision 2 | PASS |
| 119 | `TestTextKeyPolicy::test_keys_named_in_c_minus_9[sessionid]` | C-9 | C-9: the keys Session D named as leaking in revision 2 | PASS |
| 120 | `TestTextKeyPolicy::test_keys_named_in_c_minus_9[email]` | C-9 | C-9: the keys Session D named as leaking in revision 2 | PASS |
| 121 | `TestTextKeyPolicy::test_short_values_under_strong_keys_are_redacted` | C-9 | strong tier: any non-empty value, no shape test | PASS |
| 122 | `TestTextKeyPolicy::test_bare_key_stays_shape_gated` | C-9 | bare `key` remains the only weak name | PASS |
| 123 | `TestTextKeyPolicy::test_business_identifiers_survive[idempotency_key=ORD-2026-000123]` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 124 | `TestTextKeyPolicy::test_business_identifiers_survive[primary_key=RFQ-2026-000042]` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 125 | `TestTextKeyPolicy::test_business_identifiers_survive[Keyboard=KB-2026-BLACK-US]` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 126 | `TestTextKeyPolicy::test_business_identifiers_survive[Turkey:` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 127 | `TestTextKeyPolicy::test_business_identifiers_survive[token_count:` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 128 | `TestTextKeyPolicy::test_business_identifiers_survive[Bearer` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 129 | `TestTextKeyPolicy::test_business_identifiers_survive[the` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 130 | `TestTextKeyPolicy::test_business_identifiers_survive[monkey:` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 131 | `TestTextKeyPolicy::test_business_identifiers_survive[max_tokens:` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 132 | `TestTextKeyPolicy::test_business_identifiers_survive[tokenizer:` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 133 | `TestTextKeyPolicy::test_business_identifiers_survive[secretary:` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 134 | `TestTextKeyPolicy::test_business_identifiers_survive[public_key:` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 135 | `TestTextKeyPolicy::test_business_identifiers_survive[hockey:` | C-9 | false-positive guard: benign identifiers, exact equality | PASS |
| 136 | `TestTextKeyPolicy::test_text_and_structure_agree` | C-9 | C-9 parity: text and structural verdicts agree | PASS |
| 137 | `TestNestedEncodedJson::test_double_encoded[api_key]` | C-10 | C-10: two encoding levels redacted | PASS |
| 138 | `TestNestedEncodedJson::test_double_encoded[password]` | C-10 | C-10: two encoding levels redacted | PASS |
| 139 | `TestNestedEncodedJson::test_double_encoded[passphrase]` | C-10 | C-10: two encoding levels redacted | PASS |
| 140 | `TestNestedEncodedJson::test_double_encoded[session_id]` | C-10 | C-10: two encoding levels redacted | PASS |
| 141 | `TestNestedEncodedJson::test_double_encoded[token]` | C-10 | C-10: two encoding levels redacted | PASS |
| 142 | `TestNestedEncodedJson::test_multiple_levels[1]` | C-10 | C-10: 1–4 encoding levels redacted in place | PASS |
| 143 | `TestNestedEncodedJson::test_multiple_levels[2]` | C-10 | C-10: 1–4 encoding levels redacted in place | PASS |
| 144 | `TestNestedEncodedJson::test_multiple_levels[3]` | C-10 | C-10: 1–4 encoding levels redacted in place | PASS |
| 145 | `TestNestedEncodedJson::test_multiple_levels[4]` | C-10 | C-10: 1–4 encoding levels redacted in place | PASS |
| 146 | `TestNestedEncodedJson::test_nested_in_innocuous_field` | C-10 | C-10: a JSON document inside `note` is still scanned | PASS |
| 147 | `TestNestedEncodedJson::test_nested_escaped_key` | C-10 | C-10: `\\u005f`-escaped key decodes for normalisation | PASS |
| 148 | `TestNestedEncodedJson::test_escaped_key_flat` | C-10 | escaped-key regression stays fixed | PASS |
| 149 | `TestNestedEncodedJson::test_deep_encoding_fails_closed` | C-10 | C-10/F-2: 17 levels -> `[REDACTED]` | PASS |
| 150 | `TestNestedEncodedJson::test_benign_nested_text_is_untouched` | C-10 | probe ordering: `say "hello"` unchanged | PASS |
| 151 | `TestNestedEncodedJson::test_idempotent_on_nested_payloads` | C-10 | idempotence over the nested corpus | PASS |
| 152 | `TestNestedEncodedJson::test_summary_of_nested_payload` | C-10 | nested payload through `summarize_payload` | PASS |
| 153 | `TestNestedEncodedJson::test_oversized_escaped_payload_fails_closed` | C-10 | C-10: > `_MAX_NESTED_PROBE` -> withheld | PASS |
| 154 | `TestPublicApiRetained::test_email_hash_is_exported` | C-12 | C-12: `email_hash` in `__all__` | PASS |
| 155 | `TestPublicApiRetained::test_email_hash_normalises_case_and_whitespace` | C-12 | C-12: strip + lower before hashing | PASS |
| 156 | `TestPublicApiRetained::test_email_hash_is_sha256_of_the_normalised_address` | C-12 | C-12: exact sha256 equality | PASS |
| 157 | `TestPublicApiRetained::test_email_hash_differs_between_addresses` | C-12 | C-12: distinct addresses -> distinct hashes | PASS |
| 158 | `TestPublicApiRetained::test_emails_are_still_redacted` | C-12 | e-mail redaction unchanged | PASS |
| 159 | `TestPublicApiRetained::test_max_bytes_zero_is_rejected` | C-12 | `max_bytes <= 0` -> `ValueError` | PASS |
| 160 | `TestPublicApiRetained::test_max_bytes_negative_is_rejected` | C-12 | same contract for negative values | PASS |
| 161 | `TestPublicApiRetained::test_redact_payload_is_exported` | C-12 | new public name exported | PASS |
| 162 | `TestPublicApiRetained::test_summary_status_vocabulary_is_closed` | C-12 | `INLINE`/`TRUNCATED`/`WITHHELD` only | PASS |
| 163 | `TestContainerValuedKeys::test_json_object_value[credentials]` | F-1 ⟦R4⟧ | F-1: strong key `credentials` + object -> whole container replaced | PASS |
| 164 | `TestContainerValuedKeys::test_json_object_value[api_key]` | F-1 ⟦R4⟧ | F-1: strong key `api_key` + object -> whole container replaced | PASS |
| 165 | `TestContainerValuedKeys::test_json_object_value[secret]` | F-1 ⟦R4⟧ | F-1: strong key `secret` + object -> whole container replaced | PASS |
| 166 | `TestContainerValuedKeys::test_json_object_value[token]` | F-1 ⟦R4⟧ | F-1: strong key `token` + object -> whole container replaced | PASS |
| 167 | `TestContainerValuedKeys::test_json_array_value` | F-1 ⟦R4⟧ | F-1: array value -> `[REDACTED]` | PASS |
| 168 | `TestContainerValuedKeys::test_deeply_nested_container` | F-1 ⟦R4⟧ | F-1: nested container -> one marker | PASS |
| 169 | `TestContainerValuedKeys::test_python_repr_dict` | F-1 ⟦R4⟧ | F-1: repr dict via bounded bracket scan | PASS |
| 170 | `TestContainerValuedKeys::test_nested_json_string` | F-1 ⟦R4⟧ | F-1: double-encoded container; escaped quotes ignored for balance | PASS |
| 171 | `TestContainerValuedKeys::test_quoted_container_value` | F-1 ⟦R4⟧ | F-1: quoted container -> content replaced, quotes kept | PASS |
| 172 | `TestContainerValuedKeys::test_yaml_block` | F-1 ⟦R4⟧ | F-1: indented YAML mapping -> `[REDACTED]` | PASS |
| 173 | `TestContainerValuedKeys::test_yaml_list` | F-1 ⟦R4⟧ | F-1: indented YAML list -> `[REDACTED]` | PASS |
| 174 | `TestContainerValuedKeys::test_yaml_block_scalar` | F-1 ⟦R4⟧ | F-1: block scalar (pipe marker) -> `[REDACTED]` | PASS |
| 175 | `TestContainerValuedKeys::test_yaml_deep_block_preserves_following_line` | F-1 ⟦R4⟧ | F-1: block ends at the first line at or left of the key | PASS |
| 176 | `TestContainerValuedKeys::test_multiple_containers` | F-1 ⟦R4⟧ | F-1: each container replaced; benign sibling preserved | PASS |
| 177 | `TestContainerValuedKeys::test_malformed_containers_fail_closed[{"api_key":` | F-1 ⟦R4⟧ | F-1: unterminated/mismatched -> withhold to end | PASS |
| 178 | `TestContainerValuedKeys::test_malformed_containers_fail_closed[{"api_key":` | F-1 ⟦R4⟧ | F-1: unterminated/mismatched -> withhold to end | PASS |
| 179 | `TestContainerValuedKeys::test_malformed_containers_fail_closed[{"api_key":` | F-1 ⟦R4⟧ | F-1: unterminated/mismatched -> withhold to end | PASS |
| 180 | `TestContainerValuedKeys::test_oversized_container_is_withheld` | F-1 ⟦R4⟧ | F-1: > 64 KiB -> withheld | PASS |
| 181 | `TestContainerValuedKeys::test_deep_container_is_withheld` | F-1 ⟦R4⟧ | F-1: depth guard -> withheld | PASS |
| 182 | `TestContainerValuedKeys::test_container_redaction_is_idempotent` | F-1 ⟦R4⟧ | F-1: idempotence over all container shapes | PASS |
| 183 | `TestContainerValuedKeys::test_benign_containers_are_untouched` | F-1 ⟦R4⟧ | F-1: non-sensitive keys byte-identical | PASS |
| 184 | `TestContainerValuedKeys::test_inner_sensitive_key_is_still_redacted` | F-1 ⟦R4⟧ | inner sensitive key matched independently | PASS |
| 185 | `TestContainerValuedKeys::test_pydantic_dict_input_value` | F-1 ⟦R4⟧ | F-1: `input_value={...}` redacted in `str(e)` | PASS |
| 186 | `TestContainerValuedKeys::test_pydantic_nested_dict_input_value` | F-1 ⟦R4⟧ | F-1: nested `input_value` redacted | PASS |
| 187 | `TestContainerValuedKeys::test_pydantic_list_input_value` | F-1 ⟦R4⟧ | F-1: list `input_value` redacted | PASS |
| 188 | `TestContainerValuedKeys::test_pydantic_errors_are_safe_through_redact_mapping` | F-1 ⟦R4⟧ | F-3/A1: raw errors pinned, safe rendering clean | PASS |
| 189 | `TestContainerValuedKeys::test_summary_of_container_payload` | F-1 ⟦R4⟧ | F-1 through `summarize_payload` | PASS |
| 190 | `TestContainerValuedKeys::test_structural_container_values[payload0]` | F-1 ⟦R4⟧ | F-1: `redact_mapping` replaces container values wholesale | PASS |
| 191 | `TestContainerValuedKeys::test_structural_container_values[payload1]` | F-1 ⟦R4⟧ | F-1: `redact_mapping` replaces container values wholesale | PASS |
| 192 | `TestContainerValuedKeys::test_structural_container_values[payload2]` | F-1 ⟦R4⟧ | F-1: `redact_mapping` replaces container values wholesale | PASS |
| 193 | `TestContainerValuedKeys::test_structural_container_values[payload3]` | F-1 ⟦R4⟧ | F-1: `redact_mapping` replaces container values wholesale | PASS |
| 194 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[1]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 195 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[2]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 196 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[3]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 197 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[4]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 198 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[5]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 199 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[6]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 200 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[7]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 201 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[8]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 202 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[9]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 203 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[10]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 204 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[11]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 205 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[12]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 206 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[13]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 207 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[14]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 208 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[15]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 209 | `TestEscapedKeyBoundaries::test_escape_runs_one_to_sixteen_are_redacted[16]` | F-2 ⟦R4⟧ | F-2: scanner reads the run; secret redacted, key kept | PASS |
| 210 | `TestEscapedKeyBoundaries::test_deeper_escape_runs_are_contained[17]` | F-2 ⟦R4⟧ | F-2: probe triggers on the deep run; contained | PASS |
| 211 | `TestEscapedKeyBoundaries::test_deeper_escape_runs_are_contained[24]` | F-2 ⟦R4⟧ | F-2: probe triggers on the deep run; contained | PASS |
| 212 | `TestEscapedKeyBoundaries::test_deeper_escape_runs_are_contained[32]` | F-2 ⟦R4⟧ | F-2: probe triggers on the deep run; contained | PASS |
| 213 | `TestEscapedKeyBoundaries::test_deeper_escape_runs_are_contained[64]` | F-2 ⟦R4⟧ | F-2: probe triggers on the deep run; contained | PASS |
| 214 | `TestEscapedKeyBoundaries::test_deeper_escape_runs_are_contained[1000]` | F-2 ⟦R4⟧ | F-2: probe triggers on the deep run; contained | PASS |
| 215 | `TestEscapedKeyBoundaries::test_benign_escaped_key_survives[1]` | F-2 ⟦R4⟧ | F-2: non-sensitive escaped key unchanged | PASS |
| 216 | `TestEscapedKeyBoundaries::test_benign_escaped_key_survives[8]` | F-2 ⟦R4⟧ | F-2: non-sensitive escaped key unchanged | PASS |
| 217 | `TestEscapedKeyBoundaries::test_benign_escaped_key_survives[16]` | F-2 ⟦R4⟧ | F-2: non-sensitive escaped key unchanged | PASS |
| 218 | `TestEscapedKeyBoundaries::test_deep_escape_without_quotes_is_caught` | F-2 ⟦R4⟧ | F-2: probe no longer depends on an escaped quote | PASS |
| 219 | `TestEscapedKeyBoundaries::test_escape_boundary_is_sixteen` | F-2 ⟦R4⟧ | F-2: 16 in place, 17 contained — the documented boundary | PASS |

