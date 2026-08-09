/**
 * Minimal ambient type surface for the `typebox` module (pi 0.84.1 bundles
 * typebox 1.x and exposes it to extensions as a virtual module). Only the
 * builders used by the D-Swarm base extensions are declared.
 */
declare module "typebox" {
  export interface TSchema {
    [key: string]: unknown;
  }
  export type TObject<T = Record<string, unknown>> = TSchema & {
    type: "object";
    properties: T;
  };
  export type TString = TSchema & { type: "string" };
  export type TNumber = TSchema & { type: "number" };
  export type TBoolean = TSchema & { type: "boolean" };
  export type TArray<T = TSchema> = TSchema & { type: "array"; items: T };
  export type TUnion<T extends TSchema[] = TSchema[]> = TSchema & { anyOf: T };
  export type TLiteral<T extends string | number | boolean> = TSchema & { const: T };
  export type TOptional<T extends TSchema> = TSchema & { optional: true } & T;

  export interface TypeBuilder {
    Object<T extends Record<string, TSchema>>(
      properties: T,
      options?: Record<string, unknown>,
    ): TObject<T>;
    String(options?: Record<string, unknown>): TString;
    Number(options?: Record<string, unknown>): TNumber;
    Boolean(options?: Record<string, unknown>): TBoolean;
    Array<T extends TSchema>(items: T, options?: Record<string, unknown>): TArray<T>;
    Union<T extends TSchema[]>(schemas: T, options?: Record<string, unknown>): TUnion<T>;
    Literal<T extends string | number | boolean>(value: T, options?: Record<string, unknown>): TLiteral<T>;
    Optional<T extends TSchema>(schema: T, options?: Record<string, unknown>): TOptional<T>;
    Any(options?: Record<string, unknown>): TSchema;
    Null(options?: Record<string, unknown>): TSchema;
    Integer(options?: Record<string, unknown>): TSchema;
    Record<T extends TSchema>(key: TSchema, value: T, options?: Record<string, unknown>): TSchema;
  }

  export const Type: TypeBuilder;
}
