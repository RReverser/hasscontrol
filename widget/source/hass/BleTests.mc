using Toybox.Test;
using Toybox.Lang;
using Toybox.StringUtil;

// Cross-language vectors generated with custom_components/garmin_ble/protocol.py
// (the Home Assistant side). If these pass, the watch and HA agree byte for byte.
module Hass {
  (:test)
  function _hex(s) {
    return StringUtil.convertEncodedString(s, {
      :fromRepresentation => StringUtil.REPRESENTATION_STRING_HEX,
      :toRepresentation => StringUtil.REPRESENTATION_BYTE_ARRAY
    });
  }

  (:test)
  function _eq(a, b) {
    if (a.size() != b.size()) {
      return false;
    }
    for (var i = 0; i < a.size(); i++) {
      if (a[i] != b[i]) {
        return false;
      }
    }
    return true;
  }

  (:test)
  function testHmacMatchesPython(logger as Test.Logger) as Lang.Boolean {
    var key = _hex("f433cbfd79e5db1494bff956ba1f5490");
    var got = hmacSha256(key, [0x61, 0x62, 0x63]b);
    var want = _hex("e00c062a2d534116728f5d1e4d9955bba6724de5de41a78b5d79f5cfa6d3fa71");
    logger.debug("hmac " + got);
    return _eq(got, want);
  }

  (:test)
  function testCommandFrameMatchesPython(logger as Test.Logger) as Lang.Boolean {
    var key = _hex("f433cbfd79e5db1494bff956ba1f5490");
    var nonce = _hex("0102030405060708");
    var arg = new [4]b;
    arg.encodeNumber(21.5, Lang.NUMBER_FORMAT_FLOAT, { :offset => 0, :endianness => Lang.ENDIAN_BIG });
    var payload = [3, 0x11]b;
    payload.addAll(arg);
    var got = buildCommand(key, nonce, 300, OP_ACTION, payload);
    logger.debug("frame " + got);
    return got.size() <= 20 && _eq(got, _hex("042c031141ac0000187ccf4a"));
  }

  (:test)
  function testReassembleAndDecodeEntity(logger as Test.Logger) as Lang.Boolean {
    var r = new Reassembler();
    var frags = [
      "0982050111696e7075745f73656c6563742e6d6f",
      "09646502046177617903084d6f646520e29c9307",
      "8909686f6d651f617761790801310a03302e35"
    ];
    var msg = null;
    for (var i = 0; i < frags.size(); i++) {
      msg = r.feed(_hex(frags[i]));
      if (i < frags.size() - 1 && msg != null) {
        return false;
      }
    }
    if (msg == null || msg[0] != MSG_ENTITY || msg[1] != 5) {
      return false;
    }
    var body = decodeEntity(msg);
    var a = body["attributes"];
    logger.debug("body " + body);
    return body["entity_id"].equals("input_select.mode")
      && body["state"].equals("away")
      && a["friendly_name"].equals("Mode ✓")
      && a["options"].size() == 2 && a["options"][0].equals("home") && a["options"][1].equals("away")
      && a["min"] == 1.0 && a["step"] == 0.5 && a["max"] == null;
  }

  (:test)
  function testReassemblerDropsBrokenMessage(logger as Test.Logger) as Lang.Boolean {
    var r = new Reassembler();
    // first fragment of message seq 1, then its tail is lost; message seq 2 arrives whole
    if (r.feed([0x01, 0xaa, 0xbb]b) != null) {
      return false;
    }
    var m = r.feed([0x82, 0x83, 0x05]b);
    return m != null && _eq(m, [0x83, 0x05]b);
  }
}
