/**
 * Type declarations for the plain-JS script helpers.
 *
 * `scripts/db.mjs` is JavaScript so plain `node` can run it without a build
 * step; this file gives the TypeScript scripts that import it full types.
 */

export interface QueryResultRow {
  [column: string]: unknown;
}

export declare function all<T = QueryResultRow>(
  sql: string,
  params?: unknown[],
): Promise<T[]>;

export declare function first<T = QueryResultRow>(
  sql: string,
  params?: unknown[],
): Promise<T | null>;

export declare function executeScript(statements: string[]): Promise<void>;