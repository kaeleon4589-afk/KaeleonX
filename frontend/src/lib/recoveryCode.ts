const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';

export function generateRecoveryCode(): string {
  const bytes = new Uint8Array(20);
  crypto.getRandomValues(bytes);
  const chars = Array.from(bytes, value => ALPHABET[value & 31]);
  const groups = Array.from({ length: 5 }, (_, index) => chars.slice(index * 4, index * 4 + 4).join(''));
  return `KAE-${groups.join('-')}`;
}
