/**
 * Program IDL in camelCase format in order to be used in JS/TS.
 *
 * Note that this is only a type helper and is not the actual IDL. The original
 * IDL can be found at `target/idl/pump_amm.json`.
 */
export interface PumpAmm {
  address: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
  metadata: {
    name: "pumpAmm";
    version: "0.1.0";
    spec: "0.1.0";
    description: "Created with Anchor";
  };
  instructions: [
    {
      name: "adminSetCoinCreator";
      docs: ["Overrides the coin creator for a canonical pump pool"];
      discriminator: [242, 40, 117, 145, 73, 96, 105, 104];
      accounts: [
        {
          name: "adminSetCoinCreatorAuthority";
          signer: true;
          relations: ["globalConfig"];
        },
        {
          name: "globalConfig";
        },
        {
          name: "pool";
          writable: true;
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "coinCreator";
          type: "pubkey";
        },
      ];
    },
    {
      name: "adminUpdateTokenIncentives";
      discriminator: [209, 11, 115, 87, 213, 23, 124, 204];
      accounts: [
        {
          name: "admin";
          writable: true;
          signer: true;
          relations: ["globalConfig"];
        },
        {
          name: "globalConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  103,
                  108,
                  111,
                  98,
                  97,
                  108,
                  95,
                  99,
                  111,
                  110,
                  102,
                  105,
                  103,
                ];
              },
            ];
          };
        },
        {
          name: "globalVolumeAccumulator";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  103,
                  108,
                  111,
                  98,
                  97,
                  108,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
            ];
          };
        },
        {
          name: "mint";
        },
        {
          name: "globalIncentiveTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "globalVolumeAccumulator";
              },
              {
                kind: "account";
                path: "tokenProgram";
              },
              {
                kind: "account";
                path: "mint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "tokenProgram";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "startTime";
          type: "i64";
        },
        {
          name: "endTime";
          type: "i64";
        },
        {
          name: "secondsInADay";
          type: "i64";
        },
        {
          name: "dayNumber";
          type: "u64";
        },
        {
          name: "tokenSupplyPerDay";
          type: "u64";
        },
      ];
    },
    {
      name: "buy";
      docs: [
        "For cashback coins, optionally pass user_volume_accumulator_wsol_ata as remaining_accounts[0].",
        "If provided and valid, the ATA will be initialized if needed.",
      ];
      discriminator: [102, 6, 61, 18, 1, 218, 235, 234];
      accounts: [
        {
          name: "pool";
          writable: true;
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "globalConfig";
        },
        {
          name: "baseMint";
          relations: ["pool"];
        },
        {
          name: "quoteMint";
          relations: ["pool"];
        },
        {
          name: "userBaseTokenAccount";
          writable: true;
        },
        {
          name: "userQuoteTokenAccount";
          writable: true;
        },
        {
          name: "poolBaseTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "poolQuoteTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "protocolFeeRecipient";
        },
        {
          name: "protocolFeeRecipientTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "protocolFeeRecipient";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "baseTokenProgram";
        },
        {
          name: "quoteTokenProgram";
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
          address: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
        },
        {
          name: "coinCreatorVaultAta";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "coinCreatorVaultAuthority";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "coinCreatorVaultAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  99,
                  114,
                  101,
                  97,
                  116,
                  111,
                  114,
                  95,
                  118,
                  97,
                  117,
                  108,
                  116,
                ];
              },
              {
                kind: "account";
                path: "pool.coin_creator";
                account: "pool";
              },
            ];
          };
        },
        {
          name: "globalVolumeAccumulator";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  103,
                  108,
                  111,
                  98,
                  97,
                  108,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
            ];
          };
        },
        {
          name: "userVolumeAccumulator";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  117,
                  115,
                  101,
                  114,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
              {
                kind: "account";
                path: "user";
              },
            ];
          };
        },
        {
          name: "feeConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [102, 101, 101, 95, 99, 111, 110, 102, 105, 103];
              },
              {
                kind: "const";
                value: [
                  12,
                  20,
                  222,
                  252,
                  130,
                  94,
                  198,
                  118,
                  148,
                  37,
                  8,
                  24,
                  187,
                  101,
                  64,
                  101,
                  244,
                  41,
                  141,
                  49,
                  86,
                  213,
                  113,
                  180,
                  212,
                  248,
                  9,
                  12,
                  24,
                  233,
                  168,
                  99,
                ];
              },
            ];
            program: {
              kind: "account";
              path: "feeProgram";
            };
          };
        },
        {
          name: "feeProgram";
          address: "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ";
        },
      ];
      args: [
        {
          name: "baseAmountOut";
          type: "u64";
        },
        {
          name: "maxQuoteAmountIn";
          type: "u64";
        },
        {
          name: "trackVolume";
          type: {
            defined: {
              name: "optionBool";
            };
          };
        },
      ];
    },
    {
      name: "buyExactQuoteIn";
      docs: [
        "Given a budget of spendable_quote_in, buy at least min_base_amount_out",
        "Fees will be deducted from spendable_quote_in",
        "",
        "f(quote) = tokens, where tokens >= min_base_amount_out",
        "",
        "Make sure the payer has enough SOL to cover creation of the following accounts (unless already created):",
        "- protocol_fee_recipient_token_account: rent.minimum_balance(TokenAccount::LEN)",
        "- coin_creator_vault_ata: rent.minimum_balance(TokenAccount::LEN)",
        "- user_volume_accumulator: rent.minimum_balance(UserVolumeAccumulator::LEN)",
        "",
        "For cashback coins, optionally pass user_volume_accumulator_wsol_ata as remaining_accounts[0].",
        "If provided and valid, the ATA will be initialized if needed.",
      ];
      discriminator: [198, 46, 21, 82, 180, 217, 232, 112];
      accounts: [
        {
          name: "pool";
          writable: true;
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "globalConfig";
        },
        {
          name: "baseMint";
          relations: ["pool"];
        },
        {
          name: "quoteMint";
          relations: ["pool"];
        },
        {
          name: "userBaseTokenAccount";
          writable: true;
        },
        {
          name: "userQuoteTokenAccount";
          writable: true;
        },
        {
          name: "poolBaseTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "poolQuoteTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "protocolFeeRecipient";
        },
        {
          name: "protocolFeeRecipientTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "protocolFeeRecipient";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "baseTokenProgram";
        },
        {
          name: "quoteTokenProgram";
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
          address: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
        },
        {
          name: "coinCreatorVaultAta";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "coinCreatorVaultAuthority";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "coinCreatorVaultAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  99,
                  114,
                  101,
                  97,
                  116,
                  111,
                  114,
                  95,
                  118,
                  97,
                  117,
                  108,
                  116,
                ];
              },
              {
                kind: "account";
                path: "pool.coin_creator";
                account: "pool";
              },
            ];
          };
        },
        {
          name: "globalVolumeAccumulator";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  103,
                  108,
                  111,
                  98,
                  97,
                  108,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
            ];
          };
        },
        {
          name: "userVolumeAccumulator";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  117,
                  115,
                  101,
                  114,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
              {
                kind: "account";
                path: "user";
              },
            ];
          };
        },
        {
          name: "feeConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [102, 101, 101, 95, 99, 111, 110, 102, 105, 103];
              },
              {
                kind: "const";
                value: [
                  12,
                  20,
                  222,
                  252,
                  130,
                  94,
                  198,
                  118,
                  148,
                  37,
                  8,
                  24,
                  187,
                  101,
                  64,
                  101,
                  244,
                  41,
                  141,
                  49,
                  86,
                  213,
                  113,
                  180,
                  212,
                  248,
                  9,
                  12,
                  24,
                  233,
                  168,
                  99,
                ];
              },
            ];
            program: {
              kind: "account";
              path: "feeProgram";
            };
          };
        },
        {
          name: "feeProgram";
          address: "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ";
        },
      ];
      args: [
        {
          name: "spendableQuoteIn";
          type: "u64";
        },
        {
          name: "minBaseAmountOut";
          type: "u64";
        },
        {
          name: "trackVolume";
          type: {
            defined: {
              name: "optionBool";
            };
          };
        },
      ];
    },
    {
      name: "claimCashback";
      discriminator: [37, 58, 35, 126, 190, 53, 228, 197];
      accounts: [
        {
          name: "user";
          writable: true;
        },
        {
          name: "userVolumeAccumulator";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  117,
                  115,
                  101,
                  114,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
              {
                kind: "account";
                path: "user";
              },
            ];
          };
        },
        {
          name: "quoteMint";
        },
        {
          name: "quoteTokenProgram";
        },
        {
          name: "userVolumeAccumulatorWsolTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "userVolumeAccumulator";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "userWsolTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "user";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
          address: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
        },
      ];
      args: [];
    },
    {
      name: "claimTokenIncentives";
      discriminator: [16, 4, 71, 28, 204, 1, 40, 27];
      accounts: [
        {
          name: "user";
        },
        {
          name: "userAta";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "user";
              },
              {
                kind: "account";
                path: "tokenProgram";
              },
              {
                kind: "account";
                path: "mint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "globalVolumeAccumulator";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  103,
                  108,
                  111,
                  98,
                  97,
                  108,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
            ];
          };
        },
        {
          name: "globalIncentiveTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "globalVolumeAccumulator";
              },
              {
                kind: "account";
                path: "tokenProgram";
              },
              {
                kind: "account";
                path: "mint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "userVolumeAccumulator";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  117,
                  115,
                  101,
                  114,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
              {
                kind: "account";
                path: "user";
              },
            ];
          };
        },
        {
          name: "mint";
          relations: ["globalVolumeAccumulator"];
        },
        {
          name: "tokenProgram";
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
          address: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
        },
        {
          name: "payer";
          writable: true;
          signer: true;
        },
      ];
      args: [];
    },
    {
      name: "closeUserVolumeAccumulator";
      discriminator: [249, 69, 164, 218, 150, 103, 84, 138];
      accounts: [
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "userVolumeAccumulator";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  117,
                  115,
                  101,
                  114,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
              {
                kind: "account";
                path: "user";
              },
            ];
          };
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "collectCoinCreatorFee";
      discriminator: [160, 57, 89, 42, 181, 139, 43, 66];
      accounts: [
        {
          name: "quoteMint";
        },
        {
          name: "quoteTokenProgram";
        },
        {
          name: "coinCreator";
        },
        {
          name: "coinCreatorVaultAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  99,
                  114,
                  101,
                  97,
                  116,
                  111,
                  114,
                  95,
                  118,
                  97,
                  117,
                  108,
                  116,
                ];
              },
              {
                kind: "account";
                path: "coinCreator";
              },
            ];
          };
        },
        {
          name: "coinCreatorVaultAta";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "coinCreatorVaultAuthority";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "coinCreatorTokenAccount";
          writable: true;
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "createConfig";
      discriminator: [201, 207, 243, 114, 75, 111, 47, 189];
      accounts: [
        {
          name: "admin";
          writable: true;
          signer: true;
          address: "8LWu7QM2dGR1G8nKDHthckea57bkCzXyBTAKPJUBDHo8";
        },
        {
          name: "globalConfig";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  103,
                  108,
                  111,
                  98,
                  97,
                  108,
                  95,
                  99,
                  111,
                  110,
                  102,
                  105,
                  103,
                ];
              },
            ];
          };
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "lpFeeBasisPoints";
          type: "u64";
        },
        {
          name: "protocolFeeBasisPoints";
          type: "u64";
        },
        {
          name: "protocolFeeRecipients";
          type: {
            array: ["pubkey", 8];
          };
        },
        {
          name: "coinCreatorFeeBasisPoints";
          type: "u64";
        },
        {
          name: "adminSetCoinCreatorAuthority";
          type: "pubkey";
        },
      ];
    },
    {
      name: "createPool";
      discriminator: [233, 146, 209, 142, 207, 104, 64, 188];
      accounts: [
        {
          name: "pool";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108];
              },
              {
                kind: "arg";
                path: "index";
              },
              {
                kind: "account";
                path: "creator";
              },
              {
                kind: "account";
                path: "baseMint";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
          };
        },
        {
          name: "globalConfig";
        },
        {
          name: "creator";
          writable: true;
          signer: true;
        },
        {
          name: "baseMint";
        },
        {
          name: "quoteMint";
        },
        {
          name: "lpMint";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  112,
                  111,
                  111,
                  108,
                  95,
                  108,
                  112,
                  95,
                  109,
                  105,
                  110,
                  116,
                ];
              },
              {
                kind: "account";
                path: "pool";
              },
            ];
          };
        },
        {
          name: "userBaseTokenAccount";
          writable: true;
        },
        {
          name: "userQuoteTokenAccount";
          writable: true;
        },
        {
          name: "userPoolTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "creator";
              },
              {
                kind: "account";
                path: "token2022Program";
              },
              {
                kind: "account";
                path: "lpMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "poolBaseTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "pool";
              },
              {
                kind: "account";
                path: "baseTokenProgram";
              },
              {
                kind: "account";
                path: "baseMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "poolQuoteTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "pool";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "token2022Program";
          address: "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
        },
        {
          name: "baseTokenProgram";
        },
        {
          name: "quoteTokenProgram";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "index";
          type: "u16";
        },
        {
          name: "baseAmountIn";
          type: "u64";
        },
        {
          name: "quoteAmountIn";
          type: "u64";
        },
        {
          name: "coinCreator";
          type: "pubkey";
        },
        {
          name: "isMayhemMode";
          type: "bool";
        },
        {
          name: "isCashbackCoin";
          type: {
            defined: {
              name: "optionBool";
            };
          };
        },
      ];
    },
    {
      name: "deposit";
      discriminator: [242, 35, 198, 137, 82, 225, 242, 182];
      accounts: [
        {
          name: "pool";
          writable: true;
        },
        {
          name: "globalConfig";
        },
        {
          name: "user";
          signer: true;
        },
        {
          name: "baseMint";
          relations: ["pool"];
        },
        {
          name: "quoteMint";
          relations: ["pool"];
        },
        {
          name: "lpMint";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "userBaseTokenAccount";
          writable: true;
        },
        {
          name: "userQuoteTokenAccount";
          writable: true;
        },
        {
          name: "userPoolTokenAccount";
          writable: true;
        },
        {
          name: "poolBaseTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "poolQuoteTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "tokenProgram";
          address: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
        },
        {
          name: "token2022Program";
          address: "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "lpTokenAmountOut";
          type: "u64";
        },
        {
          name: "maxBaseAmountIn";
          type: "u64";
        },
        {
          name: "maxQuoteAmountIn";
          type: "u64";
        },
      ];
    },
    {
      name: "disable";
      discriminator: [185, 173, 187, 90, 216, 15, 238, 233];
      accounts: [
        {
          name: "admin";
          signer: true;
          relations: ["globalConfig"];
        },
        {
          name: "globalConfig";
          writable: true;
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "disableCreatePool";
          type: "bool";
        },
        {
          name: "disableDeposit";
          type: "bool";
        },
        {
          name: "disableWithdraw";
          type: "bool";
        },
        {
          name: "disableBuy";
          type: "bool";
        },
        {
          name: "disableSell";
          type: "bool";
        },
      ];
    },
    {
      name: "extendAccount";
      discriminator: [234, 102, 194, 203, 150, 72, 62, 229];
      accounts: [
        {
          name: "account";
          writable: true;
        },
        {
          name: "user";
          signer: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "initUserVolumeAccumulator";
      discriminator: [94, 6, 202, 115, 255, 96, 232, 183];
      accounts: [
        {
          name: "payer";
          writable: true;
          signer: true;
        },
        {
          name: "user";
        },
        {
          name: "userVolumeAccumulator";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  117,
                  115,
                  101,
                  114,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
              {
                kind: "account";
                path: "user";
              },
            ];
          };
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "migratePoolCoinCreator";
      docs: ["Migrate Pool Coin Creator to Sharing Config"];
      discriminator: [208, 8, 159, 4, 74, 175, 16, 58];
      accounts: [
        {
          name: "pool";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108];
              },
              {
                kind: "account";
                path: "pool.index";
                account: "pool";
              },
              {
                kind: "account";
                path: "pool.creator";
                account: "pool";
              },
              {
                kind: "account";
                path: "pool.base_mint";
                account: "pool";
              },
              {
                kind: "account";
                path: "pool.quote_mint";
                account: "pool";
              },
            ];
          };
        },
        {
          name: "sharingConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  115,
                  104,
                  97,
                  114,
                  105,
                  110,
                  103,
                  45,
                  99,
                  111,
                  110,
                  102,
                  105,
                  103,
                ];
              },
              {
                kind: "account";
                path: "pool.base_mint";
                account: "pool";
              },
            ];
            program: {
              kind: "const";
              value: [
                12,
                53,
                255,
                169,
                5,
                90,
                142,
                86,
                141,
                168,
                247,
                188,
                7,
                86,
                21,
                39,
                76,
                241,
                201,
                44,
                164,
                31,
                64,
                0,
                156,
                81,
                106,
                164,
                20,
                194,
                124,
                112,
              ];
            };
          };
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "sell";
      discriminator: [51, 230, 133, 164, 1, 127, 131, 173];
      accounts: [
        {
          name: "pool";
          writable: true;
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "globalConfig";
        },
        {
          name: "baseMint";
          relations: ["pool"];
        },
        {
          name: "quoteMint";
          relations: ["pool"];
        },
        {
          name: "userBaseTokenAccount";
          writable: true;
        },
        {
          name: "userQuoteTokenAccount";
          writable: true;
        },
        {
          name: "poolBaseTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "poolQuoteTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "protocolFeeRecipient";
        },
        {
          name: "protocolFeeRecipientTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "protocolFeeRecipient";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "baseTokenProgram";
        },
        {
          name: "quoteTokenProgram";
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
          address: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
        },
        {
          name: "coinCreatorVaultAta";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "coinCreatorVaultAuthority";
              },
              {
                kind: "account";
                path: "quoteTokenProgram";
              },
              {
                kind: "account";
                path: "quoteMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "coinCreatorVaultAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  99,
                  114,
                  101,
                  97,
                  116,
                  111,
                  114,
                  95,
                  118,
                  97,
                  117,
                  108,
                  116,
                ];
              },
              {
                kind: "account";
                path: "pool.coin_creator";
                account: "pool";
              },
            ];
          };
        },
        {
          name: "feeConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [102, 101, 101, 95, 99, 111, 110, 102, 105, 103];
              },
              {
                kind: "const";
                value: [
                  12,
                  20,
                  222,
                  252,
                  130,
                  94,
                  198,
                  118,
                  148,
                  37,
                  8,
                  24,
                  187,
                  101,
                  64,
                  101,
                  244,
                  41,
                  141,
                  49,
                  86,
                  213,
                  113,
                  180,
                  212,
                  248,
                  9,
                  12,
                  24,
                  233,
                  168,
                  99,
                ];
              },
            ];
            program: {
              kind: "account";
              path: "feeProgram";
            };
          };
        },
        {
          name: "feeProgram";
          address: "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ";
        },
      ];
      args: [
        {
          name: "baseAmountIn";
          type: "u64";
        },
        {
          name: "minQuoteAmountOut";
          type: "u64";
        },
      ];
    },
    {
      name: "setCoinCreator";
      docs: [
        "Sets Pool::coin_creator from Metaplex metadata creator or BondingCurve::creator",
      ];
      discriminator: [210, 149, 128, 45, 188, 58, 78, 175];
      accounts: [
        {
          name: "pool";
          writable: true;
        },
        {
          name: "metadata";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [109, 101, 116, 97, 100, 97, 116, 97];
              },
              {
                kind: "const";
                value: [
                  11,
                  112,
                  101,
                  177,
                  227,
                  209,
                  124,
                  69,
                  56,
                  157,
                  82,
                  127,
                  107,
                  4,
                  195,
                  205,
                  88,
                  184,
                  108,
                  115,
                  26,
                  160,
                  253,
                  181,
                  73,
                  182,
                  209,
                  188,
                  3,
                  248,
                  41,
                  70,
                ];
              },
              {
                kind: "account";
                path: "pool.base_mint";
                account: "pool";
              },
            ];
            program: {
              kind: "const";
              value: [
                11,
                112,
                101,
                177,
                227,
                209,
                124,
                69,
                56,
                157,
                82,
                127,
                107,
                4,
                195,
                205,
                88,
                184,
                108,
                115,
                26,
                160,
                253,
                181,
                73,
                182,
                209,
                188,
                3,
                248,
                41,
                70,
              ];
            };
          };
        },
        {
          name: "bondingCurve";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  98,
                  111,
                  110,
                  100,
                  105,
                  110,
                  103,
                  45,
                  99,
                  117,
                  114,
                  118,
                  101,
                ];
              },
              {
                kind: "account";
                path: "pool.base_mint";
                account: "pool";
              },
            ];
            program: {
              kind: "const";
              value: [
                1,
                86,
                224,
                246,
                147,
                102,
                90,
                207,
                68,
                219,
                21,
                104,
                191,
                23,
                91,
                170,
                81,
                137,
                203,
                151,
                245,
                210,
                255,
                59,
                101,
                93,
                43,
                182,
                253,
                109,
                24,
                176,
              ];
            };
          };
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "setReservedFeeRecipients";
      discriminator: [111, 172, 162, 232, 114, 89, 213, 142];
      accounts: [
        {
          name: "globalConfig";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  103,
                  108,
                  111,
                  98,
                  97,
                  108,
                  95,
                  99,
                  111,
                  110,
                  102,
                  105,
                  103,
                ];
              },
            ];
          };
        },
        {
          name: "admin";
          signer: true;
          relations: ["globalConfig"];
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "whitelistPda";
          type: "pubkey";
        },
      ];
    },
    {
      name: "syncUserVolumeAccumulator";
      discriminator: [86, 31, 192, 87, 163, 87, 79, 238];
      accounts: [
        {
          name: "user";
        },
        {
          name: "globalVolumeAccumulator";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  103,
                  108,
                  111,
                  98,
                  97,
                  108,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
            ];
          };
        },
        {
          name: "userVolumeAccumulator";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  117,
                  115,
                  101,
                  114,
                  95,
                  118,
                  111,
                  108,
                  117,
                  109,
                  101,
                  95,
                  97,
                  99,
                  99,
                  117,
                  109,
                  117,
                  108,
                  97,
                  116,
                  111,
                  114,
                ];
              },
              {
                kind: "account";
                path: "user";
              },
            ];
          };
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "toggleCashbackEnabled";
      discriminator: [115, 103, 224, 255, 189, 89, 86, 195];
      accounts: [
        {
          name: "admin";
          signer: true;
          relations: ["globalConfig"];
        },
        {
          name: "globalConfig";
          writable: true;
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "enabled";
          type: "bool";
        },
      ];
    },
    {
      name: "toggleMayhemMode";
      discriminator: [1, 9, 111, 208, 100, 31, 255, 163];
      accounts: [
        {
          name: "admin";
          signer: true;
          relations: ["globalConfig"];
        },
        {
          name: "globalConfig";
          writable: true;
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "enabled";
          type: "bool";
        },
      ];
    },
    {
      name: "transferCreatorFeesToPump";
      docs: [
        "Transfer creator fees to pump creator vault",
        "If coin creator fees are currently below rent.minimum_balance(TokenAccount::LEN)",
        "The transfer will be skipped",
      ];
      discriminator: [139, 52, 134, 85, 228, 229, 108, 241];
      accounts: [
        {
          name: "wsolMint";
          docs: ["Pump Canonical Pool are quoted in wSOL"];
        },
        {
          name: "tokenProgram";
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "coinCreator";
        },
        {
          name: "coinCreatorVaultAuthority";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  99,
                  114,
                  101,
                  97,
                  116,
                  111,
                  114,
                  95,
                  118,
                  97,
                  117,
                  108,
                  116,
                ];
              },
              {
                kind: "account";
                path: "coinCreator";
              },
            ];
          };
        },
        {
          name: "coinCreatorVaultAta";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "coinCreatorVaultAuthority";
              },
              {
                kind: "account";
                path: "tokenProgram";
              },
              {
                kind: "account";
                path: "wsolMint";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "pumpCreatorVault";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  99,
                  114,
                  101,
                  97,
                  116,
                  111,
                  114,
                  45,
                  118,
                  97,
                  117,
                  108,
                  116,
                ];
              },
              {
                kind: "account";
                path: "coinCreator";
              },
            ];
            program: {
              kind: "const";
              value: [
                1,
                86,
                224,
                246,
                147,
                102,
                90,
                207,
                68,
                219,
                21,
                104,
                191,
                23,
                91,
                170,
                81,
                137,
                203,
                151,
                245,
                210,
                255,
                59,
                101,
                93,
                43,
                182,
                253,
                109,
                24,
                176,
              ];
            };
          };
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "updateAdmin";
      discriminator: [161, 176, 40, 213, 60, 184, 179, 228];
      accounts: [
        {
          name: "admin";
          signer: true;
          relations: ["globalConfig"];
        },
        {
          name: "globalConfig";
          writable: true;
        },
        {
          name: "newAdmin";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [];
    },
    {
      name: "updateFeeConfig";
      discriminator: [104, 184, 103, 242, 88, 151, 107, 20];
      accounts: [
        {
          name: "admin";
          signer: true;
          relations: ["globalConfig"];
        },
        {
          name: "globalConfig";
          writable: true;
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "lpFeeBasisPoints";
          type: "u64";
        },
        {
          name: "protocolFeeBasisPoints";
          type: "u64";
        },
        {
          name: "protocolFeeRecipients";
          type: {
            array: ["pubkey", 8];
          };
        },
        {
          name: "coinCreatorFeeBasisPoints";
          type: "u64";
        },
        {
          name: "adminSetCoinCreatorAuthority";
          type: "pubkey";
        },
      ];
    },
    {
      name: "withdraw";
      discriminator: [183, 18, 70, 156, 148, 109, 161, 34];
      accounts: [
        {
          name: "pool";
          writable: true;
        },
        {
          name: "globalConfig";
        },
        {
          name: "user";
          signer: true;
        },
        {
          name: "baseMint";
          relations: ["pool"];
        },
        {
          name: "quoteMint";
          relations: ["pool"];
        },
        {
          name: "lpMint";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "userBaseTokenAccount";
          writable: true;
        },
        {
          name: "userQuoteTokenAccount";
          writable: true;
        },
        {
          name: "userPoolTokenAccount";
          writable: true;
        },
        {
          name: "poolBaseTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "poolQuoteTokenAccount";
          writable: true;
          relations: ["pool"];
        },
        {
          name: "tokenProgram";
          address: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
        },
        {
          name: "token2022Program";
          address: "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
        },
        {
          name: "eventAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  95,
                  95,
                  101,
                  118,
                  101,
                  110,
                  116,
                  95,
                  97,
                  117,
                  116,
                  104,
                  111,
                  114,
                  105,
                  116,
                  121,
                ];
              },
            ];
          };
        },
        {
          name: "program";
        },
      ];
      args: [
        {
          name: "lpTokenAmountIn";
          type: "u64";
        },
        {
          name: "minBaseAmountOut";
          type: "u64";
        },
        {
          name: "minQuoteAmountOut";
          type: "u64";
        },
      ];
    },
  ];
  accounts: [
    {
      name: "bondingCurve";
      discriminator: [23, 183, 248, 55, 96, 216, 172, 96];
    },
    {
      name: "feeConfig";
      discriminator: [143, 52, 146, 187, 219, 123, 76, 155];
    },
    {
      name: "globalConfig";
      discriminator: [149, 8, 156, 202, 160, 252, 176, 217];
    },
    {
      name: "globalVolumeAccumulator";
      discriminator: [202, 42, 246, 43, 142, 190, 30, 255];
    },
    {
      name: "pool";
      discriminator: [241, 154, 109, 4, 17, 177, 109, 188];
    },
    {
      name: "sharingConfig";
      discriminator: [216, 74, 9, 0, 56, 140, 93, 75];
    },
    {
      name: "userVolumeAccumulator";
      discriminator: [86, 255, 112, 14, 102, 53, 154, 250];
    },
  ];
  events: [
    {
      name: "adminSetCoinCreatorEvent";
      discriminator: [45, 220, 93, 24, 25, 97, 172, 104];
    },
    {
      name: "adminUpdateTokenIncentivesEvent";
      discriminator: [147, 250, 108, 120, 247, 29, 67, 222];
    },
    {
      name: "buyEvent";
      discriminator: [103, 244, 82, 31, 44, 245, 119, 119];
    },
    {
      name: "claimCashbackEvent";
      discriminator: [226, 214, 246, 33, 7, 242, 147, 229];
    },
    {
      name: "claimTokenIncentivesEvent";
      discriminator: [79, 172, 246, 49, 205, 91, 206, 232];
    },
    {
      name: "closeUserVolumeAccumulatorEvent";
      discriminator: [146, 159, 189, 172, 146, 88, 56, 244];
    },
    {
      name: "collectCoinCreatorFeeEvent";
      discriminator: [232, 245, 194, 238, 234, 218, 58, 89];
    },
    {
      name: "createConfigEvent";
      discriminator: [107, 52, 89, 129, 55, 226, 81, 22];
    },
    {
      name: "createPoolEvent";
      discriminator: [177, 49, 12, 210, 160, 118, 167, 116];
    },
    {
      name: "depositEvent";
      discriminator: [120, 248, 61, 83, 31, 142, 107, 144];
    },
    {
      name: "disableEvent";
      discriminator: [107, 253, 193, 76, 228, 202, 27, 104];
    },
    {
      name: "extendAccountEvent";
      discriminator: [97, 97, 215, 144, 93, 146, 22, 124];
    },
    {
      name: "initUserVolumeAccumulatorEvent";
      discriminator: [134, 36, 13, 72, 232, 101, 130, 216];
    },
    {
      name: "migratePoolCoinCreatorEvent";
      discriminator: [170, 221, 82, 199, 147, 165, 247, 46];
    },
    {
      name: "reservedFeeRecipientsEvent";
      discriminator: [43, 188, 250, 18, 221, 75, 187, 95];
    },
    {
      name: "sellEvent";
      discriminator: [62, 47, 55, 10, 165, 3, 220, 42];
    },
    {
      name: "setBondingCurveCoinCreatorEvent";
      discriminator: [242, 231, 235, 102, 65, 99, 189, 211];
    },
    {
      name: "setMetaplexCoinCreatorEvent";
      discriminator: [150, 107, 199, 123, 124, 207, 102, 228];
    },
    {
      name: "syncUserVolumeAccumulatorEvent";
      discriminator: [197, 122, 167, 124, 116, 81, 91, 255];
    },
    {
      name: "updateAdminEvent";
      discriminator: [225, 152, 171, 87, 246, 63, 66, 234];
    },
    {
      name: "updateFeeConfigEvent";
      discriminator: [90, 23, 65, 35, 62, 244, 188, 208];
    },
    {
      name: "withdrawEvent";
      discriminator: [22, 9, 133, 26, 160, 44, 71, 192];
    },
  ];
  errors: [
    {
      code: 6000;
      name: "feeBasisPointsExceedsMaximum";
    },
    {
      code: 6001;
      name: "zeroBaseAmount";
    },
    {
      code: 6002;
      name: "zeroQuoteAmount";
    },
    {
      code: 6003;
      name: "tooLittlePoolTokenLiquidity";
    },
    {
      code: 6004;
      name: "exceededSlippage";
    },
    {
      code: 6005;
      name: "invalidAdmin";
    },
    {
      code: 6006;
      name: "unsupportedBaseMint";
    },
    {
      code: 6007;
      name: "unsupportedQuoteMint";
    },
    {
      code: 6008;
      name: "invalidBaseMint";
    },
    {
      code: 6009;
      name: "invalidQuoteMint";
    },
    {
      code: 6010;
      name: "invalidLpMint";
    },
    {
      code: 6011;
      name: "allProtocolFeeRecipientsShouldBeNonZero";
    },
    {
      code: 6012;
      name: "unsortedNotUniqueProtocolFeeRecipients";
    },
    {
      code: 6013;
      name: "invalidProtocolFeeRecipient";
    },
    {
      code: 6014;
      name: "invalidPoolBaseTokenAccount";
    },
    {
      code: 6015;
      name: "invalidPoolQuoteTokenAccount";
    },
    {
      code: 6016;
      name: "buyMoreBaseAmountThanPoolReserves";
    },
    {
      code: 6017;
      name: "disabledCreatePool";
    },
    {
      code: 6018;
      name: "disabledDeposit";
    },
    {
      code: 6019;
      name: "disabledWithdraw";
    },
    {
      code: 6020;
      name: "disabledBuy";
    },
    {
      code: 6021;
      name: "disabledSell";
    },
    {
      code: 6022;
      name: "sameMint";
    },
    {
      code: 6023;
      name: "overflow";
    },
    {
      code: 6024;
      name: "truncation";
    },
    {
      code: 6025;
      name: "divisionByZero";
    },
    {
      code: 6026;
      name: "newSizeLessThanCurrentSize";
    },
    {
      code: 6027;
      name: "accountTypeNotSupported";
    },
    {
      code: 6028;
      name: "onlyCanonicalPumpPoolsCanHaveCoinCreator";
    },
    {
      code: 6029;
      name: "invalidAdminSetCoinCreatorAuthority";
    },
    {
      code: 6030;
      name: "startTimeInThePast";
    },
    {
      code: 6031;
      name: "endTimeInThePast";
    },
    {
      code: 6032;
      name: "endTimeBeforeStartTime";
    },
    {
      code: 6033;
      name: "timeRangeTooLarge";
    },
    {
      code: 6034;
      name: "endTimeBeforeCurrentDay";
    },
    {
      code: 6035;
      name: "supplyUpdateForFinishedRange";
    },
    {
      code: 6036;
      name: "dayIndexAfterEndIndex";
    },
    {
      code: 6037;
      name: "dayInActiveRange";
    },
    {
      code: 6038;
      name: "invalidIncentiveMint";
    },
    {
      code: 6039;
      name: "buyNotEnoughQuoteTokensToCoverFees";
      msg: "buy: Not enough quote tokens to cover for fees.";
    },
    {
      code: 6040;
      name: "buySlippageBelowMinBaseAmountOut";
      msg: "buy: slippage - would buy less tokens than expected min_base_amount_out";
    },
    {
      code: 6041;
      name: "mayhemModeDisabled";
    },
    {
      code: 6042;
      name: "onlyPumpPoolsMayhemMode";
    },
    {
      code: 6043;
      name: "mayhemModeInDesiredState";
    },
    {
      code: 6044;
      name: "notEnoughRemainingAccounts";
    },
    {
      code: 6045;
      name: "invalidSharingConfigBaseMint";
    },
    {
      code: 6046;
      name: "invalidSharingConfigCoinCreator";
    },
    {
      code: 6047;
      name: "coinCreatorMigratedToSharingConfig";
      msg: "coin creator has been migrated to sharing config, use pump_fees::reset_fee_sharing_config instead";
    },
    {
      code: 6048;
      name: "creatorVaultMigratedToSharingConfig";
      msg: "creator_vault has been migrated to sharing config, use pump:distribute_creator_fees instead";
    },
    {
      code: 6049;
      name: "cashbackNotEnabled";
      msg: "Cashback is disabled";
    },
    {
      code: 6050;
      name: "onlyPumpPoolsCashback";
    },
    {
      code: 6051;
      name: "cashbackNotInDesiredState";
    },
    {
      code: 6052;
      name: "cashbackEarnedDoesNotMatchTokenInVault";
    },
  ];
  types: [
    {
      name: "adminSetCoinCreatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "adminSetCoinCreatorAuthority";
            type: "pubkey";
          },
          {
            name: "baseMint";
            type: "pubkey";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "oldCoinCreator";
            type: "pubkey";
          },
          {
            name: "newCoinCreator";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "adminUpdateTokenIncentivesEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "startTime";
            type: "i64";
          },
          {
            name: "endTime";
            type: "i64";
          },
          {
            name: "dayNumber";
            type: "u64";
          },
          {
            name: "tokenSupplyPerDay";
            type: "u64";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "secondsInADay";
            type: "i64";
          },
          {
            name: "timestamp";
            type: "i64";
          },
        ];
      };
    },
    {
      name: "bondingCurve";
      type: {
        kind: "struct";
        fields: [
          {
            name: "virtualTokenReserves";
            type: "u64";
          },
          {
            name: "virtualSolReserves";
            type: "u64";
          },
          {
            name: "realTokenReserves";
            type: "u64";
          },
          {
            name: "realSolReserves";
            type: "u64";
          },
          {
            name: "tokenTotalSupply";
            type: "u64";
          },
          {
            name: "complete";
            type: "bool";
          },
          {
            name: "creator";
            type: "pubkey";
          },
          {
            name: "isMayhemMode";
            type: "bool";
          },
          {
            name: "isCashbackCoin";
            type: "bool";
          },
        ];
      };
    },
    {
      name: "buyEvent";
      docs: ['ix_name: "buy" | "buy_exact_quote_in"'];
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "baseAmountOut";
            type: "u64";
          },
          {
            name: "maxQuoteAmountIn";
            type: "u64";
          },
          {
            name: "userBaseTokenReserves";
            type: "u64";
          },
          {
            name: "userQuoteTokenReserves";
            type: "u64";
          },
          {
            name: "poolBaseTokenReserves";
            type: "u64";
          },
          {
            name: "poolQuoteTokenReserves";
            type: "u64";
          },
          {
            name: "quoteAmountIn";
            type: "u64";
          },
          {
            name: "lpFeeBasisPoints";
            type: "u64";
          },
          {
            name: "lpFee";
            type: "u64";
          },
          {
            name: "protocolFeeBasisPoints";
            type: "u64";
          },
          {
            name: "protocolFee";
            type: "u64";
          },
          {
            name: "quoteAmountInWithLpFee";
            type: "u64";
          },
          {
            name: "userQuoteAmountIn";
            type: "u64";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "userBaseTokenAccount";
            type: "pubkey";
          },
          {
            name: "userQuoteTokenAccount";
            type: "pubkey";
          },
          {
            name: "protocolFeeRecipient";
            type: "pubkey";
          },
          {
            name: "protocolFeeRecipientTokenAccount";
            type: "pubkey";
          },
          {
            name: "coinCreator";
            type: "pubkey";
          },
          {
            name: "coinCreatorFeeBasisPoints";
            type: "u64";
          },
          {
            name: "coinCreatorFee";
            type: "u64";
          },
          {
            name: "trackVolume";
            type: "bool";
          },
          {
            name: "totalUnclaimedTokens";
            type: "u64";
          },
          {
            name: "totalClaimedTokens";
            type: "u64";
          },
          {
            name: "currentSolVolume";
            type: "u64";
          },
          {
            name: "lastUpdateTimestamp";
            type: "i64";
          },
          {
            name: "minBaseAmountOut";
            type: "u64";
          },
          {
            name: "ixName";
            type: "string";
          },
          {
            name: "cashbackFeeBasisPoints";
            type: "u64";
          },
          {
            name: "cashback";
            type: "u64";
          },
        ];
      };
    },
    {
      name: "claimCashbackEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "amount";
            type: "u64";
          },
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "totalClaimed";
            type: "u64";
          },
          {
            name: "totalCashbackEarned";
            type: "u64";
          },
        ];
      };
    },
    {
      name: "claimTokenIncentivesEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "amount";
            type: "u64";
          },
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "totalClaimedTokens";
            type: "u64";
          },
          {
            name: "currentSolVolume";
            type: "u64";
          },
        ];
      };
    },
    {
      name: "closeUserVolumeAccumulatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "totalUnclaimedTokens";
            type: "u64";
          },
          {
            name: "totalClaimedTokens";
            type: "u64";
          },
          {
            name: "currentSolVolume";
            type: "u64";
          },
          {
            name: "lastUpdateTimestamp";
            type: "i64";
          },
        ];
      };
    },
    {
      name: "collectCoinCreatorFeeEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "coinCreator";
            type: "pubkey";
          },
          {
            name: "coinCreatorFee";
            type: "u64";
          },
          {
            name: "coinCreatorVaultAta";
            type: "pubkey";
          },
          {
            name: "coinCreatorTokenAccount";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "configStatus";
      type: {
        kind: "enum";
        variants: [
          {
            name: "paused";
          },
          {
            name: "active";
          },
        ];
      };
    },
    {
      name: "createConfigEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "admin";
            type: "pubkey";
          },
          {
            name: "lpFeeBasisPoints";
            type: "u64";
          },
          {
            name: "protocolFeeBasisPoints";
            type: "u64";
          },
          {
            name: "protocolFeeRecipients";
            type: {
              array: ["pubkey", 8];
            };
          },
          {
            name: "coinCreatorFeeBasisPoints";
            type: "u64";
          },
          {
            name: "adminSetCoinCreatorAuthority";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "createPoolEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "index";
            type: "u16";
          },
          {
            name: "creator";
            type: "pubkey";
          },
          {
            name: "baseMint";
            type: "pubkey";
          },
          {
            name: "quoteMint";
            type: "pubkey";
          },
          {
            name: "baseMintDecimals";
            type: "u8";
          },
          {
            name: "quoteMintDecimals";
            type: "u8";
          },
          {
            name: "baseAmountIn";
            type: "u64";
          },
          {
            name: "quoteAmountIn";
            type: "u64";
          },
          {
            name: "poolBaseAmount";
            type: "u64";
          },
          {
            name: "poolQuoteAmount";
            type: "u64";
          },
          {
            name: "minimumLiquidity";
            type: "u64";
          },
          {
            name: "initialLiquidity";
            type: "u64";
          },
          {
            name: "lpTokenAmountOut";
            type: "u64";
          },
          {
            name: "poolBump";
            type: "u8";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "lpMint";
            type: "pubkey";
          },
          {
            name: "userBaseTokenAccount";
            type: "pubkey";
          },
          {
            name: "userQuoteTokenAccount";
            type: "pubkey";
          },
          {
            name: "coinCreator";
            type: "pubkey";
          },
          {
            name: "isMayhemMode";
            type: "bool";
          },
        ];
      };
    },
    {
      name: "depositEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "lpTokenAmountOut";
            type: "u64";
          },
          {
            name: "maxBaseAmountIn";
            type: "u64";
          },
          {
            name: "maxQuoteAmountIn";
            type: "u64";
          },
          {
            name: "userBaseTokenReserves";
            type: "u64";
          },
          {
            name: "userQuoteTokenReserves";
            type: "u64";
          },
          {
            name: "poolBaseTokenReserves";
            type: "u64";
          },
          {
            name: "poolQuoteTokenReserves";
            type: "u64";
          },
          {
            name: "baseAmountIn";
            type: "u64";
          },
          {
            name: "quoteAmountIn";
            type: "u64";
          },
          {
            name: "lpMintSupply";
            type: "u64";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "userBaseTokenAccount";
            type: "pubkey";
          },
          {
            name: "userQuoteTokenAccount";
            type: "pubkey";
          },
          {
            name: "userPoolTokenAccount";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "disableEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "admin";
            type: "pubkey";
          },
          {
            name: "disableCreatePool";
            type: "bool";
          },
          {
            name: "disableDeposit";
            type: "bool";
          },
          {
            name: "disableWithdraw";
            type: "bool";
          },
          {
            name: "disableBuy";
            type: "bool";
          },
          {
            name: "disableSell";
            type: "bool";
          },
        ];
      };
    },
    {
      name: "extendAccountEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "account";
            type: "pubkey";
          },
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "currentSize";
            type: "u64";
          },
          {
            name: "newSize";
            type: "u64";
          },
        ];
      };
    },
    {
      name: "feeConfig";
      type: {
        kind: "struct";
        fields: [
          {
            name: "bump";
            type: "u8";
          },
          {
            name: "admin";
            type: "pubkey";
          },
          {
            name: "flatFees";
            type: {
              defined: {
                name: "fees";
              };
            };
          },
          {
            name: "feeTiers";
            type: {
              vec: {
                defined: {
                  name: "feeTier";
                };
              };
            };
          },
        ];
      };
    },
    {
      name: "feeTier";
      type: {
        kind: "struct";
        fields: [
          {
            name: "marketCapLamportsThreshold";
            type: "u128";
          },
          {
            name: "fees";
            type: {
              defined: {
                name: "fees";
              };
            };
          },
        ];
      };
    },
    {
      name: "fees";
      type: {
        kind: "struct";
        fields: [
          {
            name: "lpFeeBps";
            type: "u64";
          },
          {
            name: "protocolFeeBps";
            type: "u64";
          },
          {
            name: "creatorFeeBps";
            type: "u64";
          },
        ];
      };
    },
    {
      name: "globalConfig";
      type: {
        kind: "struct";
        fields: [
          {
            name: "admin";
            docs: ["The admin pubkey"];
            type: "pubkey";
          },
          {
            name: "lpFeeBasisPoints";
            type: "u64";
          },
          {
            name: "protocolFeeBasisPoints";
            type: "u64";
          },
          {
            name: "disableFlags";
            docs: [
              "Flags to disable certain functionality",
              "bit 0 - Disable create pool",
              "bit 1 - Disable deposit",
              "bit 2 - Disable withdraw",
              "bit 3 - Disable buy",
              "bit 4 - Disable sell",
            ];
            type: "u8";
          },
          {
            name: "protocolFeeRecipients";
            docs: ["Addresses of the protocol fee recipients"];
            type: {
              array: ["pubkey", 8];
            };
          },
          {
            name: "coinCreatorFeeBasisPoints";
            type: "u64";
          },
          {
            name: "adminSetCoinCreatorAuthority";
            docs: ["The admin authority for setting coin creators"];
            type: "pubkey";
          },
          {
            name: "whitelistPda";
            type: "pubkey";
          },
          {
            name: "reservedFeeRecipient";
            type: "pubkey";
          },
          {
            name: "mayhemModeEnabled";
            type: "bool";
          },
          {
            name: "reservedFeeRecipients";
            type: {
              array: ["pubkey", 7];
            };
          },
          {
            name: "isCashbackEnabled";
            type: "bool";
          },
        ];
      };
    },
    {
      name: "globalVolumeAccumulator";
      type: {
        kind: "struct";
        fields: [
          {
            name: "startTime";
            type: "i64";
          },
          {
            name: "endTime";
            type: "i64";
          },
          {
            name: "secondsInADay";
            type: "i64";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "totalTokenSupply";
            type: {
              array: ["u64", 30];
            };
          },
          {
            name: "solVolumes";
            type: {
              array: ["u64", 30];
            };
          },
        ];
      };
    },
    {
      name: "initUserVolumeAccumulatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "payer";
            type: "pubkey";
          },
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "timestamp";
            type: "i64";
          },
        ];
      };
    },
    {
      name: "migratePoolCoinCreatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "baseMint";
            type: "pubkey";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "sharingConfig";
            type: "pubkey";
          },
          {
            name: "oldCoinCreator";
            type: "pubkey";
          },
          {
            name: "newCoinCreator";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "optionBool";
      type: {
        kind: "struct";
        fields: ["bool"];
      };
    },
    {
      name: "pool";
      type: {
        kind: "struct";
        fields: [
          {
            name: "poolBump";
            type: "u8";
          },
          {
            name: "index";
            type: "u16";
          },
          {
            name: "creator";
            type: "pubkey";
          },
          {
            name: "baseMint";
            type: "pubkey";
          },
          {
            name: "quoteMint";
            type: "pubkey";
          },
          {
            name: "lpMint";
            type: "pubkey";
          },
          {
            name: "poolBaseTokenAccount";
            type: "pubkey";
          },
          {
            name: "poolQuoteTokenAccount";
            type: "pubkey";
          },
          {
            name: "lpSupply";
            docs: ["True circulating supply without burns and lock-ups"];
            type: "u64";
          },
          {
            name: "coinCreator";
            type: "pubkey";
          },
          {
            name: "isMayhemMode";
            type: "bool";
          },
          {
            name: "isCashbackCoin";
            type: "bool";
          },
        ];
      };
    },
    {
      name: "reservedFeeRecipientsEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "reservedFeeRecipient";
            type: "pubkey";
          },
          {
            name: "reservedFeeRecipients";
            type: {
              array: ["pubkey", 7];
            };
          },
        ];
      };
    },
    {
      name: "sellEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "baseAmountIn";
            type: "u64";
          },
          {
            name: "minQuoteAmountOut";
            type: "u64";
          },
          {
            name: "userBaseTokenReserves";
            type: "u64";
          },
          {
            name: "userQuoteTokenReserves";
            type: "u64";
          },
          {
            name: "poolBaseTokenReserves";
            type: "u64";
          },
          {
            name: "poolQuoteTokenReserves";
            type: "u64";
          },
          {
            name: "quoteAmountOut";
            type: "u64";
          },
          {
            name: "lpFeeBasisPoints";
            type: "u64";
          },
          {
            name: "lpFee";
            type: "u64";
          },
          {
            name: "protocolFeeBasisPoints";
            type: "u64";
          },
          {
            name: "protocolFee";
            type: "u64";
          },
          {
            name: "quoteAmountOutWithoutLpFee";
            type: "u64";
          },
          {
            name: "userQuoteAmountOut";
            type: "u64";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "userBaseTokenAccount";
            type: "pubkey";
          },
          {
            name: "userQuoteTokenAccount";
            type: "pubkey";
          },
          {
            name: "protocolFeeRecipient";
            type: "pubkey";
          },
          {
            name: "protocolFeeRecipientTokenAccount";
            type: "pubkey";
          },
          {
            name: "coinCreator";
            type: "pubkey";
          },
          {
            name: "coinCreatorFeeBasisPoints";
            type: "u64";
          },
          {
            name: "coinCreatorFee";
            type: "u64";
          },
          {
            name: "cashbackFeeBasisPoints";
            type: "u64";
          },
          {
            name: "cashback";
            type: "u64";
          },
        ];
      };
    },
    {
      name: "setBondingCurveCoinCreatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "baseMint";
            type: "pubkey";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "bondingCurve";
            type: "pubkey";
          },
          {
            name: "coinCreator";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "setMetaplexCoinCreatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "baseMint";
            type: "pubkey";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "metadata";
            type: "pubkey";
          },
          {
            name: "coinCreator";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "shareholder";
      type: {
        kind: "struct";
        fields: [
          {
            name: "address";
            type: "pubkey";
          },
          {
            name: "shareBps";
            type: "u16";
          },
        ];
      };
    },
    {
      name: "sharingConfig";
      type: {
        kind: "struct";
        fields: [
          {
            name: "bump";
            type: "u8";
          },
          {
            name: "version";
            type: "u8";
          },
          {
            name: "status";
            type: {
              defined: {
                name: "configStatus";
              };
            };
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "admin";
            type: "pubkey";
          },
          {
            name: "adminRevoked";
            type: "bool";
          },
          {
            name: "shareholders";
            type: {
              vec: {
                defined: {
                  name: "shareholder";
                };
              };
            };
          },
        ];
      };
    },
    {
      name: "syncUserVolumeAccumulatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "totalClaimedTokensBefore";
            type: "u64";
          },
          {
            name: "totalClaimedTokensAfter";
            type: "u64";
          },
          {
            name: "timestamp";
            type: "i64";
          },
        ];
      };
    },
    {
      name: "updateAdminEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "admin";
            type: "pubkey";
          },
          {
            name: "newAdmin";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "updateFeeConfigEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "admin";
            type: "pubkey";
          },
          {
            name: "lpFeeBasisPoints";
            type: "u64";
          },
          {
            name: "protocolFeeBasisPoints";
            type: "u64";
          },
          {
            name: "protocolFeeRecipients";
            type: {
              array: ["pubkey", 8];
            };
          },
          {
            name: "coinCreatorFeeBasisPoints";
            type: "u64";
          },
          {
            name: "adminSetCoinCreatorAuthority";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "userVolumeAccumulator";
      type: {
        kind: "struct";
        fields: [
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "needsClaim";
            type: "bool";
          },
          {
            name: "totalUnclaimedTokens";
            type: "u64";
          },
          {
            name: "totalClaimedTokens";
            type: "u64";
          },
          {
            name: "currentSolVolume";
            type: "u64";
          },
          {
            name: "lastUpdateTimestamp";
            type: "i64";
          },
          {
            name: "hasTotalClaimedTokens";
            type: "bool";
          },
          {
            name: "cashbackEarned";
            type: "u64";
          },
          {
            name: "totalCashbackClaimed";
            type: "u64";
          },
        ];
      };
    },
    {
      name: "withdrawEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "lpTokenAmountIn";
            type: "u64";
          },
          {
            name: "minBaseAmountOut";
            type: "u64";
          },
          {
            name: "minQuoteAmountOut";
            type: "u64";
          },
          {
            name: "userBaseTokenReserves";
            type: "u64";
          },
          {
            name: "userQuoteTokenReserves";
            type: "u64";
          },
          {
            name: "poolBaseTokenReserves";
            type: "u64";
          },
          {
            name: "poolQuoteTokenReserves";
            type: "u64";
          },
          {
            name: "baseAmountOut";
            type: "u64";
          },
          {
            name: "quoteAmountOut";
            type: "u64";
          },
          {
            name: "lpMintSupply";
            type: "u64";
          },
          {
            name: "pool";
            type: "pubkey";
          },
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "userBaseTokenAccount";
            type: "pubkey";
          },
          {
            name: "userQuoteTokenAccount";
            type: "pubkey";
          },
          {
            name: "userPoolTokenAccount";
            type: "pubkey";
          },
        ];
      };
    },
  ];
}
