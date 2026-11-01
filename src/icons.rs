//! Original revision-6 icons, retained as integer-generated static assets.
fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &byte in data {
        crc ^= u32::from(byte);
        for _ in 0..8 {
            let mask = 0u32.wrapping_sub(crc & 1);
            crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
        }
    }
    !crc
}
fn adler32(data: &[u8]) -> u32 {
    let (mut a, mut b) = (1u32, 0u32);
    for &byte in data {
        a = (a + u32::from(byte)) % 65521;
        b = (b + a) % 65521;
    }
    (b << 16) | a
}
fn png_chunk(kind: &[u8; 4], data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let sum = crc32(&out[4..]);
    out.extend_from_slice(&sum.to_be_bytes());
    out
}
fn icon_pixel(x: i32, y: i32, size: i32) -> [u8; 4] {
    let dx = x - size / 2;
    let dy = y - size / 2;
    let d2 = dx * dx + dy * dy;
    let core = (size / 10) * (size / 10);
    let inner = (size * 18 / 64) * (size * 18 / 64);
    let outer = (size * 24 / 64) * (size * 24 / 64);
    if d2 <= core.max(4) {
        [0xe8, 0xb0, 0x5c, 255]
    } else if d2 >= inner && d2 <= outer {
        [0x62, 0xe6, 0xc9, 255]
    } else {
        [0x05, 0x06, 0x0a, 255]
    }
}
pub fn apple_touch_png() -> Vec<u8> {
    let mut raw = Vec::new();
    for y in 0..64 {
        raw.push(0);
        for x in 0..64 {
            raw.extend_from_slice(&icon_pixel(x, y, 64));
        }
    }
    let mut zlib = vec![0x78, 0x01, 0x01];
    let len = raw.len() as u16;
    zlib.extend_from_slice(&len.to_le_bytes());
    zlib.extend_from_slice(&(!len).to_le_bytes());
    zlib.extend_from_slice(&raw);
    zlib.extend_from_slice(&adler32(&raw).to_be_bytes());
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&64u32.to_be_bytes());
    ihdr.extend_from_slice(&64u32.to_be_bytes());
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]);
    let mut png = vec![0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a];
    png.extend(png_chunk(b"IHDR", &ihdr));
    png.extend(png_chunk(b"IDAT", &zlib));
    png.extend(png_chunk(b"IEND", &[]));
    png
}
pub fn favicon_ico() -> Vec<u8> {
    let mut pixels = Vec::new();
    for y in (0..16).rev() {
        for x in 0..16 {
            let p = icon_pixel(x * 4, y * 4, 64);
            pixels.extend_from_slice(&[p[2], p[1], p[0], p[3]]);
        }
    }
    let mut out = vec![0, 0, 1, 0, 1, 0, 16, 16, 0, 0, 1, 0, 32, 0];
    out.extend_from_slice(&1128u32.to_le_bytes());
    out.extend_from_slice(&22u32.to_le_bytes());
    out.extend_from_slice(&40u32.to_le_bytes());
    out.extend_from_slice(&16i32.to_le_bytes());
    out.extend_from_slice(&32i32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&32u16.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&1024u32.to_le_bytes());
    out.extend_from_slice(&[0; 16]);
    out.extend(pixels);
    out.extend_from_slice(&[0; 64]);
    out
}
pub const SVG: &str = r##"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#05060a"/><circle cx="16" cy="16" r="9" fill="none" stroke="#62e6c9" stroke-width="2"/><circle cx="16" cy="16" r="3" fill="#e8b05c"/></svg>"##;
pub const MANIFEST: &str = r##"{"name":"8c.is","short_name":"8c.is","start_url":"/","display":"standalone","background_color":"#05060a","theme_color":"#05060a","icons":[{"src":"/apple-touch-icon.png","sizes":"64x64","type":"image/png"},{"src":"/favicon.svg","sizes":"any","type":"image/svg+xml"}]}"##;
