/** Only the versioned audit proves this particular operation was not dispatched. */
export function isUndispatchedSchemaFailure(operation: { tool_version: string; error_code: string | null }): boolean {
  return (operation.tool_version === 'invalid-tool-audit-v1' || operation.tool_version === 'invalid-tool-audit-v9-v1') && operation.error_code === 'INVALID_ARGUMENTS'
}
