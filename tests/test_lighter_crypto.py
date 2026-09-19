import base64

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from lib.lighter_crypto import LighterSigner
from lib.lighter_crypto.field import (
    FP5_ONE,
    GOLDILOCKS_ORDER,
    fp5_from_bytes,
    fp5_inv_or_zero,
    fp5_mul,
    fp5_to_bytes,
    make_fp5,
)
from lib.lighter_crypto.poseidon import poseidon_hash_fp5, poseidon_permute

# Vectors were generated with lighter-go 8854554703385766c73410b43e93075447f36a4c and
# poseidon_crypto ca0ad1bc8dfe822c61ba7556d1d4d5bcc2ea4a26. The WASM clock was fixed and its
# random scalar source returned 1. The synthetic seed below is not used by a real account.
SEED = bytes(range(132)).hex()
PUBLIC_KEY = "78daf54caf19602f90834f5a48b47a4253956d1bb94ee733c931ae3a814cc772cf414b3862f1d405"
PRIVATE_KEY = "dff01c54ae20d3906fe0f4c4cca5dd6df50ba7f62bd577437c7cc7aa1a4db14782bef4cd75d8a564"
CHANGE_PUB_KEY_HASH = (
    "e93f5260f3fb82548e3bc28aa38d5915bbf77f950275040537501a035052a68e86dc8063d17650fd"
)
CHANGE_PUB_KEY_SIGNATURE = "186fc00e108f433a15e026a91fc5eac7fb53be099693437fa5d5c716298a97105f94cea4ddc9f175f8b52ee52f2c29b0adcd343047a3ded504e647987d3bb851f51b86afe5a96b1ccf47d2bf69de9f40"
AUTH_SIGNATURE = "1982c4a98927f2108bdaa1c10cdd68354f6daace3304e5be78322dc1be7af5e400be7cfdab0ed3487e4fe2063a9449af065988f87e7e63ca9efaea88409812488e1d32132ce7e5ec4b0f891007cf4671"


def test_fp5_roundtrip():
    value = make_fp5((1, 2, 3, 4, 5))
    assert fp5_from_bytes(fp5_to_bytes(value)) == value
    assert fp5_mul(value, fp5_inv_or_zero(value)) == FP5_ONE


def test_fp5_noncanonical():
    data = GOLDILOCKS_ORDER.to_bytes(8, "little") + bytes(32)
    with pytest.raises(ValueError, match="non-canonical"):
        fp5_from_bytes(data)


def test_poseidon_permutation():
    values = (
        5417613058500526590,
        2481548824842427254,
        6473243198879784792,
        1720313757066167274,
        2806320291675974571,
        7407976414706455446,
        1105257841424046885,
        7613435757403328049,
        3376066686066811538,
        5888575799323675710,
        6689309723188675948,
        2468250420241012720,
    )
    expected = (
        5364184781011389007,
        15309475861242939136,
        5983386513087443499,
        886942118604446276,
        14903657885227062600,
        7742650891575941298,
        1962182278500985790,
        10213480816595178755,
        3510799061817443836,
        4610029967627506430,
        7566382334276534836,
        2288460879362380348,
    )
    assert poseidon_permute(values) == expected


def test_poseidon_hash():
    result = poseidon_hash_fp5(
        (
            3451004116618606032,
            11263134342958518251,
            10957204882857370932,
            5369763041201481933,
            7695734348563036858,
            1393419330378128434,
            7387917082382606332,
        )
    )
    assert result == (
        17992684813643984528,
        5243896189906434327,
        7705560276311184368,
        2785244775876017560,
        14449776097783372302,
    )


def test_key_derivation():
    signer = LighterSigner(SEED)
    assert signer.public_key_hex == PUBLIC_KEY
    assert signer.change_pub_key_message(28553, 0, 42) == (
        "Register Lighter Account\n\n"
        f"pubkey: 0x{PUBLIC_KEY}\n"
        "nonce: 0x000000000000002a\n"
        "account index: 0x0000000000006f89\n"
        "api key index: 0x0000000000000000\n"
        "Only sign this message for a trusted client!"
    )


def test_change_key_vector(monkeypatch):
    monkeypatch.setattr("lib.lighter_crypto.signer.secrets.randbelow", lambda _: 1)
    signer = LighterSigner(SEED)
    tx = signer.sign_change_pub_key(
        account_index=28553,
        api_key_index=0,
        nonce=42,
        expired_at=1700000600000,
        l1_signature="0x" + "11" * 65,
    )

    assert tx.tx_type == 8
    assert tx.tx_hash == CHANGE_PUB_KEY_HASH
    assert base64.b64decode(tx.info["PubKey"]) == bytes.fromhex(PUBLIC_KEY)
    assert base64.b64decode(tx.info["Sig"]).hex() == CHANGE_PUB_KEY_SIGNATURE
    assert tx.info == {
        "AccountIndex": 28553,
        "ApiKeyIndex": 0,
        "PubKey": "eNr1TK8ZYC+Qg09aSLR6QlOVbRu5TuczyTGuOoFMx3LPQUs4YvHUBQ==",
        "L1Sig": "0x" + "11" * 65,
        "ExpiredAt": 1700000600000,
        "Nonce": 42,
        "Sig": "GG/ADhCPQzoV4CapH8Xqx/tTvgmWk0N/pdXHFimKlxBflM6k3cnxdfi1LuUvLCmwrc00MEej3tUE5keYfTu4UfUbhq/lqWscz0fSv2nen0A=",
        "L2TxAttributes": {"4": 1},
    }


def test_integrator_vector(monkeypatch):
    monkeypatch.setattr("lib.lighter_crypto.signer.secrets.randbelow", lambda _: 1)
    signer = LighterSigner(SEED)
    tx = signer.sign_approve_integrator(
        account_index=748157,
        api_key_index=0,
        integrator_account_index=281474976496476,
        max_perps_taker_fee=1000,
        max_perps_maker_fee=1000,
        max_spot_taker_fee=10000,
        max_spot_maker_fee=10000,
        approval_expiry=1821244471614,
        nonce=1789708471615,
        expired_at=1789709080163,
        l1_signature="0x" + "11" * 65,
    )

    assert tx.tx_type == 45
    assert tx.tx_hash == (
        "57a07a04f3d8eb2abd3a421c8ed0adc58c75077ad9c88ee83f873139c3f96de73262ed1a30a44f47"
    )
    assert signer.approve_integrator_message(
        748157,
        0,
        1789708471615,
        281474976496476,
        1000,
        1000,
        10000,
        10000,
        1821244471614,
    ) == (
        "Approve Integrator\n\n"
        "nonce: 0x000001a0b2f00d3f\n"
        "account index: 0x00000000000b6a7d\n"
        "api key index: 0x0000000000000000\n"
        "integrator account index: 0x0000fffffffcbb5c\n"
        "max perps taker fee: 0x00000000000003e8\n"
        "max perps maker fee: 0x00000000000003e8\n"
        "max spot taker fee: 0x0000000000002710\n"
        "max spot maker fee: 0x0000000000002710\n"
        "approval expiry: 0x000001a80aa1393e\n"
        "chainId: 0x0000000000000130\n"
        "Only sign this message for a trusted client!"
    )


def test_robinhood_fast_withdraw_vector(monkeypatch):
    monkeypatch.setattr("lib.lighter_crypto.signer.secrets.randbelow", lambda _: 1)
    signer = LighterSigner(SEED, 466324)
    address = "0x731a64a386a01B898963Ced8141c699B93b0bDE5"
    memo = bytes.fromhex(address[2:]) + bytes(12)
    signature = (
        "0x510bbddb4fb3fe71cdf2dfde22339a9c0c75cfe1d5c9b947f50b0df7c0d34ac3"
        "24aa7f912fed27c4a83d45dcb8ced02817cb35142aa44276b8f55e56eb3aaf941b"
    )
    message = signer.transfer_message(
        account_index=28799,
        api_key_index=0,
        to_account_index=5,
        asset_index=3,
        from_route_type=0,
        to_route_type=0,
        amount=20_000_000,
        usdc_fee=0,
        memo=memo,
        nonce=1789728338987,
    )
    tx = signer.sign_transfer(
        account_index=28799,
        api_key_index=0,
        to_account_index=5,
        asset_index=3,
        from_route_type=0,
        to_route_type=0,
        amount=20_000_000,
        usdc_fee=0,
        memo=memo,
        nonce=1789728338987,
        expired_at=1789728948526,
        l1_signature=signature,
    )

    recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    assert recovered == address
    assert tx.tx_type == 12
    assert tx.tx_hash == (
        "a697abd57e04ad2e26e9c6d15025788f618b110b7ef5bb56048c83c2f9bccd8e92741dfbc9b845d8"
    )
    assert tx.info["Memo"] == list(memo)
    assert tx.info["L1Sig"] == signature


def test_order_vectors(monkeypatch):
    monkeypatch.setattr("lib.lighter_crypto.signer.secrets.randbelow", lambda _: 1)
    signer = LighterSigner(SEED)
    txs = [
        signer.sign_create_order(
            28553,
            0,
            1,
            123456789,
            12,
            795351,
            False,
            1,
            0,
            False,
            0,
            0,
            42,
            1789438636469,
        ),
        signer.sign_cancel_order(28553, 0, 1, 123456789, 43, 1789438636469),
        signer.sign_cancel_all_orders(28553, 0, 44, 1789438636470),
        signer.sign_update_leverage(28553, 0, 1, 1000, 45, 1789438636470),
    ]

    assert [tx.tx_type for tx in txs] == [14, 15, 16, 20]
    assert [tx.tx_hash for tx in txs] == [
        "54388996df82a8971c26b906326d0377f55042f93005b7dd94c5cdd74b4945ae88fe03f349255c2c",
        "e0f26cce0bcecc72478310782b62fca6abb849cb58a8191d35b07729ec2a92dc025b9edadea4403d",
        "91dca55a1823bf8607d20acecddfc63a1713fa5d30a835615d3c358a6a28bba6d03feb1d18c3ffd1",
        "28f7becbfb06341fd82ee706314be6ca36b2dfc76558ca1c52cb1630178ce8fa63ae79dd3b6c1378",
    ]


def test_auth_vector(monkeypatch):
    monkeypatch.setattr("lib.lighter_crypto.signer.secrets.randbelow", lambda _: 1)
    auth = LighterSigner(SEED).create_auth_token(28553, 0, 1700003601)
    assert auth.deadline == 1700003601
    assert auth.token == f"1700003601:28553:0:{AUTH_SIGNATURE}"


@pytest.mark.parametrize(
    "seed,error",
    [
        ("zz", "hexadecimal"),
        (bytes(31).hex(), "at least 32 bytes"),
    ],
    ids=["hex", "short"],
)
def test_seed_validation(seed, error):
    with pytest.raises(ValueError, match=error):
        LighterSigner(seed)


def test_secrets_hidden(monkeypatch):
    monkeypatch.setattr("lib.lighter_crypto.signer.secrets.randbelow", lambda _: 1)
    signer = LighterSigner(SEED)
    tx = signer.sign_change_pub_key(28553, 0, 42, 1700000600000, "11" * 65)
    auth = signer.create_auth_token(28553, 0, 1700003601)

    assert SEED not in repr(signer)
    assert PRIVATE_KEY not in repr(signer)
    assert AUTH_SIGNATURE not in repr(auth)
    assert "11" * 65 not in repr(tx)
