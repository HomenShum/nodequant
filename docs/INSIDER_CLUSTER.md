# SEC Form 4 cluster-purchase experiment

The signal, calendar-time portfolio, costs, folds and gate were frozen before SEC archive or price bytes. The corrected policy is `sha256:365b6de904b7b69fb4880482381b75c0d043613df3c84021b6d7715649318b33`; evaluator `sha256:95d0b64db6e875e6eab0315f6c8736c77a154ed74561b0c4e9e7953631f09f49`.

Status is `BLOCKED_SEC_ARCHIVE_TRANSPORT`. Official SEC index and schema pages are readable, but bounded programmatic archive retrieval returned HTTP 403 and the signed-in Chrome page did not emit or persist a download. No third-party mirror was substituted. SEC archive bytes and all price bytes remain unread.

One pre-byte evaluator defect was found and corrected: an existing frozen price file could be caught and mislabeled as a per-symbol provider failure instead of aborting the run. The corrected path aborts globally, and all five scenario tests pass. The earlier policy hash is superseded rather than silently rewritten in the decision record.
