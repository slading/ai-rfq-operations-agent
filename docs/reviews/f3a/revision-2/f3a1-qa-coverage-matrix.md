# F3-A1 — QA Coverage Matrix (all 89 tests)

Gate: `docs/reviews/f3a/test_redaction_adversarial.py` at commit `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`.
Prototype: `f3a1-prototype.py` (revision 2). Baseline commit `d5611eef`.
Command: `PYTHONPATH=src:<dir> .venv/bin/python -m pytest test_redaction_adversarial.py -q`.

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

**Totals: 89/89 passing, 0 failing.**

Reproduce:

```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
PYTHONPATH=src:<dir containing f3a1-prototype.py> \
  .venv/bin/python -m pytest test_redaction_adversarial.py -q
```

