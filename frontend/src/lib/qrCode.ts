const QR_VERSION = 5;
const QR_SIZE = 17 + QR_VERSION * 4;
const DATA_CODEWORDS = 108;
const ECC_CODEWORDS = 26;
const MAX_BYTE_LENGTH = 106;

type Matrix = boolean[][];

function gfMultiply(x: number, y: number) {
  let result = 0;
  let a = x;
  let b = y;
  while (b > 0) {
    if (b & 1) result ^= a;
    b >>>= 1;
    a <<= 1;
    if (a & 0x100) a ^= 0x11d;
  }
  return result;
}

function rsGenerator(degree: number) {
  let generator = [1];
  let alpha = 1;
  for (let i = 0; i < degree; i += 1) {
    if (i === 0) alpha = 1;
    else if (i === 1) alpha = 2;
    else alpha = gfMultiply(alpha, 2);
    const next = new Array(generator.length + 1).fill(0);
    generator.forEach((coefficient, index) => {
      next[index] ^= coefficient;
      next[index + 1] ^= gfMultiply(coefficient, alpha);
    });
    generator = next;
  }
  return generator;
}

function reedSolomonRemainder(data: number[], degree: number) {
  const generator = rsGenerator(degree);
  let remainder = new Array(degree).fill(0);
  data.forEach((byte) => {
    const factor = byte ^ remainder[0];
    remainder = remainder.slice(1);
    remainder.push(0);
    for (let i = 0; i < degree; i += 1) {
      remainder[i] ^= gfMultiply(generator[i + 1], factor);
    }
  });
  return remainder;
}

function appendBits(target: number[], value: number, count: number) {
  for (let i = count - 1; i >= 0; i -= 1) target.push((value >>> i) & 1);
}

function createCodewords(text: string) {
  const bytes = Array.from(new TextEncoder().encode(text));
  if (bytes.length > MAX_BYTE_LENGTH) throw new Error('El enlace de referido es demasiado largo para el QR local.');

  const bits: number[] = [];
  appendBits(bits, 0b0100, 4); // byte mode
  appendBits(bits, bytes.length, 8); // versions 1-9
  bytes.forEach((byte) => appendBits(bits, byte, 8));

  const capacity = DATA_CODEWORDS * 8;
  const terminator = Math.min(4, Math.max(0, capacity - bits.length));
  for (let i = 0; i < terminator; i += 1) bits.push(0);
  while (bits.length % 8) bits.push(0);

  const data: number[] = [];
  for (let i = 0; i < bits.length; i += 8) {
    let value = 0;
    for (let j = 0; j < 8; j += 1) value = (value << 1) | bits[i + j];
    data.push(value);
  }
  const pads = [0xec, 0x11];
  let padIndex = 0;
  while (data.length < DATA_CODEWORDS) {
    data.push(pads[padIndex % 2]);
    padIndex += 1;
  }
  return [...data, ...reedSolomonRemainder(data, ECC_CODEWORDS)];
}

function roundedFinder(matrix: Matrix, reserved: Matrix, row: number, col: number) {
  for (let dy = -1; dy <= 7; dy += 1) {
    for (let dx = -1; dx <= 7; dx += 1) {
      const y = row + dy;
      const x = col + dx;
      if (y < 0 || y >= QR_SIZE || x < 0 || x >= QR_SIZE) continue;
      const dark =
        (dy >= 0 && dy <= 6 && (dx === 0 || dx === 6)) ||
        (dx >= 0 && dx <= 6 && (dy === 0 || dy === 6)) ||
        (dy >= 2 && dy <= 4 && dx >= 2 && dx <= 4);
      matrix[y][x] = dark;
      reserved[y][x] = true;
    }
  }
}

function alignmentPattern(matrix: Matrix, reserved: Matrix, centerY: number, centerX: number) {
  for (let dy = -2; dy <= 2; dy += 1) {
    for (let dx = -2; dx <= 2; dx += 1) {
      matrix[centerY + dy][centerX + dx] = Math.abs(dx) === 2 || Math.abs(dy) === 2 || (dx === 0 && dy === 0);
      reserved[centerY + dy][centerX + dx] = true;
    }
  }
}

function baseMatrix() {
  const matrix: Matrix = Array.from({ length: QR_SIZE }, () => new Array(QR_SIZE).fill(false));
  const reserved: Matrix = Array.from({ length: QR_SIZE }, () => new Array(QR_SIZE).fill(false));

  roundedFinder(matrix, reserved, 0, 0);
  roundedFinder(matrix, reserved, 0, QR_SIZE - 7);
  roundedFinder(matrix, reserved, QR_SIZE - 7, 0);
  alignmentPattern(matrix, reserved, 30, 30); // Version 5 alignment centers are [6, 30].

  for (let i = 8; i < QR_SIZE - 8; i += 1) {
    if (!reserved[6][i]) {
      matrix[6][i] = i % 2 === 0;
      reserved[6][i] = true;
    }
    if (!reserved[i][6]) {
      matrix[i][6] = i % 2 === 0;
      reserved[i][6] = true;
    }
  }

  for (let i = 0; i < 15; i += 1) {
    let y: number;
    let x: number;
    if (i < 6) {
      y = i;
      x = 8;
    } else if (i < 8) {
      y = i + 1;
      x = 8;
    } else {
      y = QR_SIZE - 15 + i;
      x = 8;
    }
    reserved[y][x] = true;

    if (i < 8) {
      y = 8;
      x = QR_SIZE - i - 1;
    } else if (i < 9) {
      y = 8;
      x = 15 - i;
    } else {
      y = 8;
      x = 15 - i - 1;
    }
    reserved[y][x] = true;
  }

  matrix[QR_SIZE - 8][8] = true;
  reserved[QR_SIZE - 8][8] = true;
  return { matrix, reserved };
}

function maskCondition(mask: number, row: number, col: number) {
  switch (mask) {
    case 0: return (row + col) % 2 === 0;
    case 1: return row % 2 === 0;
    case 2: return col % 3 === 0;
    case 3: return (row + col) % 3 === 0;
    case 4: return (Math.floor(row / 2) + Math.floor(col / 3)) % 2 === 0;
    case 5: return (row * col) % 2 + (row * col) % 3 === 0;
    case 6: return ((row * col) % 2 + (row * col) % 3) % 2 === 0;
    case 7: return ((row * col) % 3 + (row + col) % 2) % 2 === 0;
    default: return false;
  }
}

function formatBits(mask: number) {
  const data = (1 << 3) | mask; // Error correction level L = 01.
  let remainder = data << 10;
  const generator = 0x537;
  const bitLength = (value: number) => (value === 0 ? 0 : Math.floor(Math.log2(value)) + 1);
  while (bitLength(remainder) >= bitLength(generator)) {
    remainder ^= generator << (bitLength(remainder) - bitLength(generator));
  }
  return ((data << 10) | remainder) ^ 0x5412;
}

function buildMatrix(text: string, mask: number) {
  const { matrix, reserved } = baseMatrix();
  const bits: number[] = [];
  createCodewords(text).forEach((byte) => appendBits(bits, byte, 8));
  let bitIndex = 0;
  let upward = true;

  for (let col = QR_SIZE - 1; col > 0; col -= 2) {
    if (col === 6) col -= 1;
    for (let offset = 0; offset < QR_SIZE; offset += 1) {
      const row = upward ? QR_SIZE - 1 - offset : offset;
      for (const x of [col, col - 1]) {
        if (reserved[row][x]) continue;
        let dark = Boolean(bits[bitIndex] ?? 0);
        bitIndex += 1;
        if (maskCondition(mask, row, x)) dark = !dark;
        matrix[row][x] = dark;
      }
    }
    upward = !upward;
  }

  const info = formatBits(mask);
  for (let i = 0; i < 15; i += 1) {
    const dark = Boolean((info >>> i) & 1);
    let y: number;
    let x: number;
    if (i < 6) {
      y = i;
      x = 8;
    } else if (i < 8) {
      y = i + 1;
      x = 8;
    } else {
      y = QR_SIZE - 15 + i;
      x = 8;
    }
    matrix[y][x] = dark;

    if (i < 8) {
      y = 8;
      x = QR_SIZE - i - 1;
    } else if (i < 9) {
      y = 8;
      x = 15 - i;
    } else {
      y = 8;
      x = 15 - i - 1;
    }
    matrix[y][x] = dark;
  }
  matrix[QR_SIZE - 8][8] = true;
  return matrix;
}

function matrixPenalty(matrix: Matrix) {
  let penalty = 0;
  const transpose = matrix[0].map((_, index) => matrix.map((row) => row[index]));

  for (const rows of [matrix, transpose]) {
    rows.forEach((row) => {
      let runLength = 1;
      let previous = row[0];
      for (let i = 1; i < row.length; i += 1) {
        if (row[i] === previous) runLength += 1;
        else {
          if (runLength >= 5) penalty += 3 + runLength - 5;
          previous = row[i];
          runLength = 1;
        }
      }
      if (runLength >= 5) penalty += 3 + runLength - 5;
    });
  }

  for (let row = 0; row < QR_SIZE - 1; row += 1) {
    for (let col = 0; col < QR_SIZE - 1; col += 1) {
      const value = matrix[row][col];
      if (matrix[row][col + 1] === value && matrix[row + 1][col] === value && matrix[row + 1][col + 1] === value) penalty += 3;
    }
  }

  const finderLike = [true, false, true, true, true, false, true];
  for (const rows of [matrix, transpose]) {
    rows.forEach((row) => {
      for (let i = 0; i <= row.length - 7; i += 1) {
        const matches = finderLike.every((value, index) => row[i + index] === value);
        if (!matches) continue;
        const before = i >= 4 && row.slice(i - 4, i).every((value) => !value);
        const after = i + 11 <= row.length && row.slice(i + 7, i + 11).every((value) => !value);
        if (before || after) penalty += 40;
      }
    });
  }

  const dark = matrix.reduce((sum, row) => sum + row.filter(Boolean).length, 0);
  const darkPercent = dark * 100 / (QR_SIZE * QR_SIZE);
  penalty += Math.floor(Math.abs(darkPercent - 50) / 5) * 10;
  return penalty;
}

export function createQrMatrix(text: string) {
  let best = buildMatrix(text, 0);
  let bestPenalty = matrixPenalty(best);
  for (let mask = 1; mask < 8; mask += 1) {
    const candidate = buildMatrix(text, mask);
    const penalty = matrixPenalty(candidate);
    if (penalty < bestPenalty) {
      best = candidate;
      bestPenalty = penalty;
    }
  }
  return best;
}

export function drawQrCode(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  size: number,
) {
  const matrix = createQrMatrix(text);
  const quiet = 4;
  const modules = matrix.length + quiet * 2;
  const moduleSize = Math.max(1, Math.floor(size / modules));
  const renderSize = moduleSize * modules;
  const offsetX = x + Math.floor((size - renderSize) / 2);
  const offsetY = y + Math.floor((size - renderSize) / 2);

  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(offsetX, offsetY, renderSize, renderSize);
  ctx.fillStyle = '#000000';
  matrix.forEach((row, rowIndex) => {
    row.forEach((dark, colIndex) => {
      if (!dark) return;
      ctx.fillRect(
        offsetX + (colIndex + quiet) * moduleSize,
        offsetY + (rowIndex + quiet) * moduleSize,
        moduleSize,
        moduleSize,
      );
    });
  });
  ctx.restore();
}

export function createQrDataUrl(text: string, size = 180) {
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('No fue posible crear el código QR.');
  drawQrCode(ctx, text, 0, 0, size);
  return canvas.toDataURL('image/png');
}
