"""Malformed input must fail loudly, not cryptically.

Both entry points here decode bytes that arrived over a network.
``deserialize_function`` runs inside a worker on a payload the coordinator
handed it; ``decode_result`` runs on the *submitting* machine on bytes a
worker produced. Neither gets to choose its input.

The contract locked in here is narrow, and it is about the shape of a failure
rather than about security: for any input whatsoever, each function either
returns something usable or raises a subclass of ``Exception`` carrying a
message a human can act on. It must never hang, never take the interpreter
down, and never raise outside the ``Exception`` hierarchy — a ``SystemExit``
escaping a decode would pass straight through every ``except Exception`` in
the worker loop and kill the worker over a single bad task.

Deliberately deterministic. The corpus is fixed and the random cases are
seeded, so a failure reproduces exactly instead of appearing in CI once and
never again. Fuzzing that cannot be replayed is a rumour, not a test.

This is emphatically NOT a claim that decoding untrusted input is safe.
``decode_result`` unpickles, and unpickling is arbitrary code execution — see
the warning block in ``gpumesh/serializer.py``, and ``--strict``. A crafted
payload that executes code has already done so before any assertion below
runs.
"""

import base64
import json
import random

import pytest

from gpumesh import serializer


def _valid_function_payload() -> str:
    """A real serialized function, to mutate into invalid ones."""
    def sample(n):
        return {"n": n, "sq": n * n}

    return serializer.serialize_function(sample)


# Hand-written malformed inputs, each aimed at one step of the decode.
MALFORMED_FUNCTIONS = [
    "",                          # empty
    "!!!not base64 at all!!!",   # fails at b64decode
    "YQ==",                      # valid base64, one byte: no length prefix
    "AAAAAA==",                  # four zero bytes: zero-length metadata
    # Length prefix claims far more metadata than the payload carries.
    base64.b64encode((10_000).to_bytes(4, "big") + b"{}").decode(),
    # Well-formed frame; metadata is valid JSON but not an object.
    base64.b64encode((2).to_bytes(4, "big") + b"[]").decode(),
    # Valid JSON object, unknown method.
    base64.b64encode(
        len(b'{"method":"telepathy"}').to_bytes(4, "big")
        + b'{"method":"telepathy"}'
    ).decode(),
    # Says cloudpickle, carries garbage where the pickle should be.
    base64.b64encode(
        len(b'{"method":"cloudpickle","func_name":"f"}').to_bytes(4, "big")
        + b'{"method":"cloudpickle","func_name":"f"}'
        + b"\x00\xff\x00\xff not a pickle"
    ).decode(),
    # Says source, carries none.
    base64.b64encode(
        len(b'{"method":"source","func_name":"f"}').to_bytes(4, "big")
        + b'{"method":"source","func_name":"f"}'
    ).decode(),
    # Source that is not valid Python.
    base64.b64encode(
        len(b'{"method":"source","func_name":"f","source":"def ("}').to_bytes(4, "big")
        + b'{"method":"source","func_name":"f","source":"def ("}'
    ).decode(),
]

MALFORMED_RESULTS = [
    {serializer.RESULT_ENVELOPE_KEY: {"encoding": "cloudpickle",
                                      "value": "!!!not-base64"}},
    {serializer.RESULT_ENVELOPE_KEY: {"encoding": "cloudpickle",
                                      "value": base64.b64encode(
                                          b"\x00 not a pickle").decode()}},
    {serializer.RESULT_ENVELOPE_KEY: {"encoding": "cloudpickle"}},  # no value
    {serializer.RESULT_ENVELOPE_KEY: {"encoding": "json"}},         # no value
    {serializer.RESULT_ENVELOPE_KEY: {"encoding": 42}},             # bad type
]


def _assert_fails_cleanly(call):
    """Run *call*; require either a value or an ordinary, readable Exception."""
    try:
        call()
    except Exception as exc:
        # This message is what somebody reads on a worker they may not own, so
        # an empty one is a real defect: it turns a diagnosable failure into
        # "something went wrong somewhere".
        assert str(exc).strip(), f"{type(exc).__name__} carried no message"
    except BaseException as exc:
        # Outside Exception means every `except Exception` in the worker loop
        # misses it, and one malformed task takes the whole worker down.
        raise AssertionError(
            f"{type(exc).__name__} escapes the Exception hierarchy"
        ) from exc


class TestMalformedFunctionPayloads:

    @pytest.mark.parametrize("payload", MALFORMED_FUNCTIONS)
    def test_hand_written_corpus_fails_cleanly(self, payload):
        _assert_fails_cleanly(lambda: serializer.deserialize_function(payload))

    def test_truncation_at_every_length_fails_cleanly(self):
        """Every prefix of a valid payload — the shape a dropped connection makes."""
        valid = _valid_function_payload()
        # Stepping rather than testing all few-thousand prefixes: the
        # interesting boundaries are the 4-byte length prefix and the
        # metadata/pickle seam, and a stride still crosses both.
        for end in range(0, len(valid), 7):
            _assert_fails_cleanly(
                lambda p=valid[:end]: serializer.deserialize_function(p)
            )

    def test_bit_flips_fail_cleanly(self):
        """Corruption in flight: one byte of a valid payload replaced."""
        raw = bytearray(base64.b64decode(_valid_function_payload()))
        rng = random.Random(20260902)
        for _ in range(200):
            mutated = bytearray(raw)
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
            encoded = base64.b64encode(bytes(mutated)).decode()
            _assert_fails_cleanly(
                lambda p=encoded: serializer.deserialize_function(p)
            )

    def test_random_bytes_fail_cleanly(self):
        """Whatever else turns up on the wire."""
        rng = random.Random(20260902)
        for _ in range(200):
            blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64)))
            encoded = base64.b64encode(blob).decode()
            _assert_fails_cleanly(
                lambda p=encoded: serializer.deserialize_function(p)
            )


class TestMalformedResultEnvelopes:

    @pytest.mark.parametrize("payload", MALFORMED_RESULTS)
    def test_hand_written_corpus_fails_cleanly(self, payload):
        _assert_fails_cleanly(lambda: serializer.decode_result(payload))

    @pytest.mark.parametrize("payload", [
        None, 0, 1.5, "a string", [], ["list"], {}, {"unrelated": "keys"},
        {serializer.RESULT_ENVELOPE_KEY: "not a dict"},
        {serializer.RESULT_ENVELOPE_KEY: None},
    ])
    def test_non_envelopes_pass_through_untouched(self, payload):
        """Script tasks emit plain JSON, and old workers emit no envelope.

        Anything that is not an envelope has to come back unchanged, or script
        results and results from older workers stop working.
        """
        assert serializer.decode_result(payload) == payload

    def test_strict_mode_refuses_before_touching_the_bytes(self):
        """Under --strict a hostile pickle must not be decoded to be rejected.

        The refusal has to rest on the envelope's ``encoding`` field alone. Were
        it reached by way of the pickle, the code execution strict mode exists
        to prevent would already have happened.
        """
        envelope = {serializer.RESULT_ENVELOPE_KEY: {
            "encoding": "cloudpickle", "value": "!!!not-base64"}}
        with pytest.raises(serializer.UntrustedResultError):
            serializer.decode_result(envelope, strict=True)

    def test_random_envelopes_fail_cleanly(self):
        rng = random.Random(20260902)
        encodings = ["json", "cloudpickle", "", "pickle", None, 7]
        for _ in range(200):
            blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 48)))
            envelope = {serializer.RESULT_ENVELOPE_KEY: {
                "encoding": rng.choice(encodings),
                "value": base64.b64encode(blob).decode(),
            }}
            _assert_fails_cleanly(lambda p=envelope: serializer.decode_result(p))


def test_a_valid_round_trip_still_works():
    """The guard against a fuzz suite that passes because everything raises."""
    def sample(n):
        return {"n": n, "sq": n * n}

    rebuilt = serializer.deserialize_function(serializer.serialize_function(sample))
    assert rebuilt(n=4) == {"n": 4, "sq": 16}

    encoded = serializer.encode_result({"ok": True})
    assert json.loads(json.dumps(encoded))  # the envelope stays JSON-safe
    assert serializer.decode_result(encoded) == {"ok": True}
