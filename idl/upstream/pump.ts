/**
 * Program IDL in camelCase format in order to be used in JS/TS.
 *
 * Note that this is only a type helper and is not the actual IDL. The original
 * IDL can be found at `target/idl/pump.json`.
 */
export interface Pump {
  address: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
  metadata: {
    name: "pump";
    version: "0.1.0";
    spec: "0.1.0";
    description: "Created with Anchor";
  };
  instructions: [
    {
      name: "adminSetCreator";
      docs: [
        "Allows Global::admin_set_creator_authority to override the bonding curve creator",
      ];
      discriminator: [69, 25, 171, 142, 57, 239, 13, 4];
      accounts: [
        {
          name: "adminSetCreatorAuthority";
          signer: true;
          relations: ["global"];
        },
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "mint";
        },
        {
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
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
      args: [
        {
          name: "creator";
          type: "pubkey";
        },
      ];
    },
    {
      name: "adminSetIdlAuthority";
      discriminator: [8, 217, 96, 231, 144, 104, 192, 5];
      accounts: [
        {
          name: "authority";
          signer: true;
          relations: ["global"];
        },
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "idlAccount";
          writable: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "programSigner";
          pda: {
            seeds: [];
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
      args: [
        {
          name: "idlAuthority";
          type: "pubkey";
        },
      ];
    },
    {
      name: "adminUpdateTokenIncentives";
      discriminator: [209, 11, 115, 87, 213, 23, 124, 204];
      accounts: [
        {
          name: "authority";
          writable: true;
          signer: true;
          relations: ["global"];
        },
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
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
          name: "pumpTokenSupplyPerDay";
          type: "u64";
        },
      ];
    },
    {
      name: "buy";
      docs: ["Buys tokens from a bonding curve."];
      discriminator: [102, 6, 61, 18, 1, 218, 235, 234];
      accounts: [
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "feeRecipient";
          writable: true;
        },
        {
          name: "mint";
        },
        {
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
              },
            ];
          };
        },
        {
          name: "associatedBondingCurve";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "bondingCurve";
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
          name: "associatedUser";
          writable: true;
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "tokenProgram";
        },
        {
          name: "creatorVault";
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
                path: "bonding_curve.creator";
                account: "bondingCurve";
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
          address: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
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
          name: "amount";
          type: "u64";
        },
        {
          name: "maxSolCost";
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
      name: "buyExactSolIn";
      docs: [
        "Given a budget of spendable SOL, buy at least min_tokens_out tokens.",
        "Fees are deducted from spendable_sol_in.",
        "",
        "# Quote formulas",
        "Where:",
        "- total_fee_bps = protocol_fee_bps + creator_fee_bps (creator_fee_bps is 0 if no creator)",
        "- floor(a/b) = a / b (integer division)",
        "- ceil(a/b) = (a + b - 1) / b",
        "",
        "SOL → tokens quote",
        "To calculate tokens_out for a given spendable_sol_in:",
        "1. net_sol = floor(spendable_sol_in * 10_000 / (10_000 + total_fee_bps))",
        "2. fees = ceil(net_sol * protocol_fee_bps / 10_000) + ceil(net_sol * creator_fee_bps / 10_000) (creator_fee_bps is 0 if no creator)",
        "3. if net_sol + fees > spendable_sol_in: net_sol = net_sol - (net_sol + fees - spendable_sol_in)",
        "4. tokens_out = floor((net_sol - 1) * virtual_token_reserves / (virtual_sol_reserves + net_sol - 1))",
        "",
        "Reverse quote (tokens → SOL)",
        "To calculate spendable_sol_in for a desired number of tokens:",
        "1. net_sol = ceil(tokens * virtual_sol_reserves / (virtual_token_reserves - tokens)) + 1",
        "2. spendable_sol_in = ceil(net_sol * (10_000 + total_fee_bps) / 10_000)",
        "",
        "Rent",
        "Separately make sure the instruction's payer has enough SOL to cover rent for:",
        "- creator_vault: rent.minimum_balance(0)",
        "- user_volume_accumulator: rent.minimum_balance(UserVolumeAccumulator::LEN)",
      ];
      discriminator: [56, 252, 116, 8, 158, 223, 205, 95];
      accounts: [
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "feeRecipient";
          writable: true;
        },
        {
          name: "mint";
        },
        {
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
              },
            ];
          };
        },
        {
          name: "associatedBondingCurve";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "bondingCurve";
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
          name: "associatedUser";
          writable: true;
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "tokenProgram";
        },
        {
          name: "creatorVault";
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
                path: "bonding_curve.creator";
                account: "bondingCurve";
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
          address: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
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
          name: "spendableSolIn";
          type: "u64";
        },
        {
          name: "minTokensOut";
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
          address: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
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
          address: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
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
      name: "collectCreatorFee";
      docs: [
        "Collects creator_fee from creator_vault to the coin creator account",
      ];
      discriminator: [20, 22, 86, 123, 198, 28, 219, 132];
      accounts: [
        {
          name: "creator";
          writable: true;
        },
        {
          name: "creatorVault";
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
                path: "creator";
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
      name: "create";
      docs: ["Creates a new coin and bonding curve."];
      discriminator: [24, 30, 200, 40, 5, 28, 7, 119];
      accounts: [
        {
          name: "mint";
          writable: true;
          signer: true;
        },
        {
          name: "mintAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  109,
                  105,
                  110,
                  116,
                  45,
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
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
              },
            ];
          };
        },
        {
          name: "associatedBondingCurve";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "bondingCurve";
              },
              {
                kind: "const";
                value: [
                  6,
                  221,
                  246,
                  225,
                  215,
                  101,
                  161,
                  147,
                  217,
                  203,
                  225,
                  70,
                  206,
                  235,
                  121,
                  172,
                  28,
                  180,
                  133,
                  237,
                  95,
                  91,
                  55,
                  145,
                  58,
                  140,
                  245,
                  133,
                  126,
                  255,
                  0,
                  169,
                ];
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
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "mplTokenMetadata";
          address: "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s";
        },
        {
          name: "metadata";
          writable: true;
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
                path: "mint";
              },
            ];
            program: {
              kind: "account";
              path: "mplTokenMetadata";
            };
          };
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "tokenProgram";
          address: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "rent";
          address: "SysvarRent111111111111111111111111111111111";
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
          name: "name";
          type: "string";
        },
        {
          name: "symbol";
          type: "string";
        },
        {
          name: "uri";
          type: "string";
        },
        {
          name: "creator";
          type: "pubkey";
        },
      ];
    },
    {
      name: "createV2";
      docs: ["Creates a new spl-22 coin and bonding curve."];
      discriminator: [214, 144, 76, 236, 95, 139, 49, 180];
      accounts: [
        {
          name: "mint";
          writable: true;
          signer: true;
        },
        {
          name: "mintAuthority";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  109,
                  105,
                  110,
                  116,
                  45,
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
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
              },
            ];
          };
        },
        {
          name: "associatedBondingCurve";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "bondingCurve";
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
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "tokenProgram";
          address: "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "mayhemProgramId";
          writable: true;
          address: "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e";
        },
        {
          name: "globalParams";
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
                  45,
                  112,
                  97,
                  114,
                  97,
                  109,
                  115,
                ];
              },
            ];
            program: {
              kind: "const";
              value: [
                5,
                42,
                229,
                215,
                167,
                218,
                167,
                36,
                166,
                234,
                176,
                167,
                41,
                84,
                145,
                133,
                90,
                212,
                160,
                103,
                22,
                96,
                103,
                76,
                78,
                3,
                69,
                89,
                128,
                61,
                101,
                163,
              ];
            };
          };
        },
        {
          name: "solVault";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [115, 111, 108, 45, 118, 97, 117, 108, 116];
              },
            ];
            program: {
              kind: "const";
              value: [
                5,
                42,
                229,
                215,
                167,
                218,
                167,
                36,
                166,
                234,
                176,
                167,
                41,
                84,
                145,
                133,
                90,
                212,
                160,
                103,
                22,
                96,
                103,
                76,
                78,
                3,
                69,
                89,
                128,
                61,
                101,
                163,
              ];
            };
          };
        },
        {
          name: "mayhemState";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  109,
                  97,
                  121,
                  104,
                  101,
                  109,
                  45,
                  115,
                  116,
                  97,
                  116,
                  101,
                ];
              },
              {
                kind: "account";
                path: "mint";
              },
            ];
            program: {
              kind: "const";
              value: [
                5,
                42,
                229,
                215,
                167,
                218,
                167,
                36,
                166,
                234,
                176,
                167,
                41,
                84,
                145,
                133,
                90,
                212,
                160,
                103,
                22,
                96,
                103,
                76,
                78,
                3,
                69,
                89,
                128,
                61,
                101,
                163,
              ];
            };
          };
        },
        {
          name: "mayhemTokenVault";
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
          name: "name";
          type: "string";
        },
        {
          name: "symbol";
          type: "string";
        },
        {
          name: "uri";
          type: "string";
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
          name: "isCashbackEnabled";
          type: {
            defined: {
              name: "optionBool";
            };
          };
        },
      ];
    },
    {
      name: "distributeCreatorFees";
      docs: [
        "Distributes creator fees to shareholders based on their share percentages",
        "The creator vault needs to have at least the minimum distributable amount to distribute fees",
        "This can be checked with the get_minimum_distributable_fee instruction",
      ];
      discriminator: [165, 114, 103, 0, 121, 206, 247, 81];
      accounts: [
        {
          name: "mint";
          relations: ["sharingConfig"];
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
                path: "mint";
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
                path: "mint";
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
          name: "creatorVault";
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
                path: "bonding_curve.creator";
                account: "bondingCurve";
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
          address: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
        },
      ];
      args: [];
      returns: {
        defined: {
          name: "distributeCreatorFeesEvent";
        };
      };
    },
    {
      name: "extendAccount";
      docs: ["Extends the size of program-owned accounts"];
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
      name: "getMinimumDistributableFee";
      docs: [
        "Permissionless instruction to check the minimum required fees for distribution",
        "Returns the minimum required balance from the creator_vault and whether distribution can proceed",
      ];
      discriminator: [117, 225, 127, 202, 134, 95, 68, 35];
      accounts: [
        {
          name: "mint";
          relations: ["sharingConfig"];
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
                path: "mint";
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
                path: "mint";
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
          name: "creatorVault";
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
                path: "bonding_curve.creator";
                account: "bondingCurve";
              },
            ];
          };
        },
      ];
      args: [];
      returns: {
        defined: {
          name: "minimumDistributableFeeEvent";
        };
      };
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
      name: "initialize";
      docs: ["Creates the global state."];
      discriminator: [175, 175, 109, 31, 13, 152, 155, 237];
      accounts: [
        {
          name: "global";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
      ];
      args: [];
    },
    {
      name: "migrate";
      docs: ["Migrates liquidity to pump_amm if the bonding curve is complete"];
      discriminator: [155, 234, 231, 146, 236, 158, 162, 30];
      accounts: [
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "withdrawAuthority";
          writable: true;
          relations: ["global"];
        },
        {
          name: "mint";
        },
        {
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
              },
            ];
          };
        },
        {
          name: "associatedBondingCurve";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "bondingCurve";
              },
              {
                kind: "account";
                path: "mint";
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
          name: "user";
          signer: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "tokenProgram";
          address: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
        },
        {
          name: "pumpAmm";
          address: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
        },
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
                kind: "const";
                value: [0, 0];
              },
              {
                kind: "account";
                path: "poolAuthority";
              },
              {
                kind: "account";
                path: "mint";
              },
              {
                kind: "account";
                path: "wsolMint";
              },
            ];
            program: {
              kind: "account";
              path: "pumpAmm";
            };
          };
        },
        {
          name: "poolAuthority";
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
                  45,
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
              {
                kind: "account";
                path: "mint";
              },
            ];
          };
        },
        {
          name: "poolAuthorityMintAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "poolAuthority";
              },
              {
                kind: "account";
                path: "mint";
              },
              {
                kind: "account";
                path: "mint";
              },
            ];
            program: {
              kind: "account";
              path: "associatedTokenProgram";
            };
          };
        },
        {
          name: "poolAuthorityWsolAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "poolAuthority";
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
              kind: "account";
              path: "associatedTokenProgram";
            };
          };
        },
        {
          name: "ammGlobalConfig";
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
            program: {
              kind: "account";
              path: "pumpAmm";
            };
          };
        },
        {
          name: "wsolMint";
          address: "So11111111111111111111111111111111111111112";
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
            program: {
              kind: "account";
              path: "pumpAmm";
            };
          };
        },
        {
          name: "userPoolTokenAccount";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "poolAuthority";
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
              kind: "account";
              path: "associatedTokenProgram";
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
                path: "mint";
              },
              {
                kind: "account";
                path: "mint";
              },
            ];
            program: {
              kind: "account";
              path: "associatedTokenProgram";
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
                path: "tokenProgram";
              },
              {
                kind: "account";
                path: "wsolMint";
              },
            ];
            program: {
              kind: "account";
              path: "associatedTokenProgram";
            };
          };
        },
        {
          name: "token2022Program";
          address: "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
        },
        {
          name: "associatedTokenProgram";
          address: "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
        },
        {
          name: "pumpAmmEventAuthority";
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
            program: {
              kind: "account";
              path: "pumpAmm";
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
      name: "migrateBondingCurveCreator";
      discriminator: [87, 124, 52, 191, 52, 38, 214, 232];
      accounts: [
        {
          name: "mint";
          relations: ["sharingConfig"];
        },
        {
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
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
                path: "mint";
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
      docs: [
        "Sells tokens into a bonding curve.",
        "For cashback coins, optionally pass user_volume_accumulator as remaining_accounts[0].",
        "If provided and valid, creator_fee goes to user_volume_accumulator.",
        "Otherwise, falls back to transferring creator_fee to creator_vault.",
      ];
      discriminator: [51, 230, 133, 164, 1, 127, 131, 173];
      accounts: [
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "feeRecipient";
          writable: true;
        },
        {
          name: "mint";
        },
        {
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
              },
            ];
          };
        },
        {
          name: "associatedBondingCurve";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "bondingCurve";
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
          name: "associatedUser";
          writable: true;
        },
        {
          name: "user";
          writable: true;
          signer: true;
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
        {
          name: "creatorVault";
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
                path: "bonding_curve.creator";
                account: "bondingCurve";
              },
            ];
          };
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
          address: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
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
          name: "amount";
          type: "u64";
        },
        {
          name: "minSolOutput";
          type: "u64";
        },
      ];
    },
    {
      name: "setCreator";
      docs: [
        "Allows Global::set_creator_authority to set the bonding curve creator from Metaplex metadata or input argument",
      ];
      discriminator: [254, 148, 255, 112, 207, 142, 170, 165];
      accounts: [
        {
          name: "setCreatorAuthority";
          signer: true;
          relations: ["global"];
        },
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "mint";
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
                path: "mint";
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
          writable: true;
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
                path: "mint";
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
      args: [
        {
          name: "creator";
          type: "pubkey";
        },
      ];
    },
    {
      name: "setMayhemVirtualParams";
      discriminator: [61, 169, 188, 191, 153, 149, 42, 97];
      accounts: [
        {
          name: "solVaultAuthority";
          writable: true;
          signer: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [115, 111, 108, 45, 118, 97, 117, 108, 116];
              },
            ];
            program: {
              kind: "const";
              value: [
                5,
                42,
                229,
                215,
                167,
                218,
                167,
                36,
                166,
                234,
                176,
                167,
                41,
                84,
                145,
                133,
                90,
                212,
                160,
                103,
                22,
                96,
                103,
                76,
                78,
                3,
                69,
                89,
                128,
                61,
                101,
                163,
              ];
            };
          };
        },
        {
          name: "mayhemTokenVault";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "solVaultAuthority";
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
          name: "mint";
        },
        {
          name: "global";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "bondingCurve";
          writable: true;
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
                path: "mint";
              },
            ];
          };
        },
        {
          name: "tokenProgram";
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
      args: [];
    },
    {
      name: "setMetaplexCreator";
      docs: [
        "Syncs the bonding curve creator with the Metaplex metadata creator if it exists",
      ];
      discriminator: [138, 96, 174, 217, 48, 85, 197, 246];
      accounts: [
        {
          name: "mint";
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
                path: "mint";
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
          writable: true;
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
                path: "mint";
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
      name: "setParams";
      docs: ["Sets the global state parameters."];
      discriminator: [27, 234, 178, 52, 147, 2, 187, 141];
      accounts: [
        {
          name: "global";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "authority";
          writable: true;
          signer: true;
          relations: ["global"];
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
          name: "initialVirtualTokenReserves";
          type: "u64";
        },
        {
          name: "initialVirtualSolReserves";
          type: "u64";
        },
        {
          name: "initialRealTokenReserves";
          type: "u64";
        },
        {
          name: "tokenTotalSupply";
          type: "u64";
        },
        {
          name: "feeBasisPoints";
          type: "u64";
        },
        {
          name: "withdrawAuthority";
          type: "pubkey";
        },
        {
          name: "enableMigrate";
          type: "bool";
        },
        {
          name: "poolMigrationFee";
          type: "u64";
        },
        {
          name: "creatorFeeBasisPoints";
          type: "u64";
        },
        {
          name: "setCreatorAuthority";
          type: "pubkey";
        },
        {
          name: "adminSetCreatorAuthority";
          type: "pubkey";
        },
      ];
    },
    {
      name: "setReservedFeeRecipients";
      discriminator: [111, 172, 162, 232, 114, 89, 213, 142];
      accounts: [
        {
          name: "global";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "authority";
          signer: true;
          relations: ["global"];
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
          name: "global";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "authority";
          writable: true;
          signer: true;
          relations: ["global"];
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
      name: "toggleCreateV2";
      discriminator: [28, 255, 230, 240, 172, 107, 203, 171];
      accounts: [
        {
          name: "global";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "authority";
          writable: true;
          signer: true;
          relations: ["global"];
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
          name: "global";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "authority";
          writable: true;
          signer: true;
          relations: ["global"];
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
      name: "updateGlobalAuthority";
      discriminator: [227, 181, 74, 196, 208, 21, 97, 213];
      accounts: [
        {
          name: "global";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [103, 108, 111, 98, 97, 108];
              },
            ];
          };
        },
        {
          name: "authority";
          signer: true;
          relations: ["global"];
        },
        {
          name: "newAuthority";
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
      name: "global";
      discriminator: [167, 232, 232, 177, 200, 108, 114, 127];
    },
    {
      name: "globalVolumeAccumulator";
      discriminator: [202, 42, 246, 43, 142, 190, 30, 255];
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
      name: "adminSetCreatorEvent";
      discriminator: [64, 69, 192, 104, 29, 30, 25, 107];
    },
    {
      name: "adminSetIdlAuthorityEvent";
      discriminator: [245, 59, 70, 34, 75, 185, 109, 92];
    },
    {
      name: "adminUpdateTokenIncentivesEvent";
      discriminator: [147, 250, 108, 120, 247, 29, 67, 222];
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
      name: "collectCreatorFeeEvent";
      discriminator: [122, 2, 127, 1, 14, 191, 12, 175];
    },
    {
      name: "completeEvent";
      discriminator: [95, 114, 97, 156, 212, 46, 152, 8];
    },
    {
      name: "completePumpAmmMigrationEvent";
      discriminator: [189, 233, 93, 185, 92, 148, 234, 148];
    },
    {
      name: "createEvent";
      discriminator: [27, 114, 169, 77, 222, 235, 99, 118];
    },
    {
      name: "distributeCreatorFeesEvent";
      discriminator: [165, 55, 129, 112, 4, 179, 202, 40];
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
      name: "migrateBondingCurveCreatorEvent";
      discriminator: [155, 167, 104, 220, 213, 108, 243, 3];
    },
    {
      name: "minimumDistributableFeeEvent";
      discriminator: [168, 216, 132, 239, 235, 182, 49, 52];
    },
    {
      name: "reservedFeeRecipientsEvent";
      discriminator: [43, 188, 250, 18, 221, 75, 187, 95];
    },
    {
      name: "setCreatorEvent";
      discriminator: [237, 52, 123, 37, 245, 251, 72, 210];
    },
    {
      name: "setMetaplexCreatorEvent";
      discriminator: [142, 203, 6, 32, 127, 105, 191, 162];
    },
    {
      name: "setParamsEvent";
      discriminator: [223, 195, 159, 246, 62, 48, 143, 131];
    },
    {
      name: "syncUserVolumeAccumulatorEvent";
      discriminator: [197, 122, 167, 124, 116, 81, 91, 255];
    },
    {
      name: "tradeEvent";
      discriminator: [189, 219, 127, 211, 78, 230, 97, 238];
    },
    {
      name: "updateGlobalAuthorityEvent";
      discriminator: [182, 195, 137, 42, 35, 206, 207, 247];
    },
    {
      name: "updateMayhemVirtualParamsEvent";
      discriminator: [117, 123, 228, 182, 161, 168, 220, 214];
    },
  ];
  errors: [
    {
      code: 6000;
      name: "notAuthorized";
      msg: "The given account is not authorized to execute this instruction.";
    },
    {
      code: 6001;
      name: "alreadyInitialized";
      msg: "The program is already initialized.";
    },
    {
      code: 6002;
      name: "tooMuchSolRequired";
      msg: "slippage: Too much SOL required to buy the given amount of tokens.";
    },
    {
      code: 6003;
      name: "tooLittleSolReceived";
      msg: "slippage: Too little SOL received to sell the given amount of tokens.";
    },
    {
      code: 6004;
      name: "mintDoesNotMatchBondingCurve";
      msg: "The mint does not match the bonding curve.";
    },
    {
      code: 6005;
      name: "bondingCurveComplete";
      msg: "The bonding curve has completed and liquidity migrated to raydium.";
    },
    {
      code: 6006;
      name: "bondingCurveNotComplete";
      msg: "The bonding curve has not completed.";
    },
    {
      code: 6007;
      name: "notInitialized";
      msg: "The program is not initialized.";
    },
    {
      code: 6008;
      name: "withdrawTooFrequent";
      msg: "Withdraw too frequent";
    },
    {
      code: 6009;
      name: "newSizeShouldBeGreaterThanCurrentSize";
      msg: "new_size should be > current_size";
    },
    {
      code: 6010;
      name: "accountTypeNotSupported";
      msg: "Account type not supported";
    },
    {
      code: 6011;
      name: "initialRealTokenReservesShouldBeLessThanTokenTotalSupply";
      msg: "initial_real_token_reserves should be less than token_total_supply";
    },
    {
      code: 6012;
      name: "initialVirtualTokenReservesShouldBeGreaterThanInitialRealTokenReserves";
      msg: "initial_virtual_token_reserves should be greater than initial_real_token_reserves";
    },
    {
      code: 6013;
      name: "feeBasisPointsGreaterThanMaximum";
      msg: "fee_basis_points greater than maximum";
    },
    {
      code: 6014;
      name: "allZerosWithdrawAuthority";
      msg: "Withdraw authority cannot be set to System Program ID";
    },
    {
      code: 6015;
      name: "poolMigrationFeeShouldBeLessThanFinalRealSolReserves";
      msg: "pool_migration_fee should be less than final_real_sol_reserves";
    },
    {
      code: 6016;
      name: "poolMigrationFeeShouldBeGreaterThanCreatorFeePlusMaxMigrateFees";
      msg: "pool_migration_fee should be greater than creator_fee + MAX_MIGRATE_FEES";
    },
    {
      code: 6017;
      name: "disabledWithdraw";
      msg: "Migrate instruction is disabled";
    },
    {
      code: 6018;
      name: "disabledMigrate";
      msg: "Migrate instruction is disabled";
    },
    {
      code: 6019;
      name: "invalidCreator";
      msg: "Invalid creator pubkey";
    },
    {
      code: 6020;
      name: "buyZeroAmount";
      msg: "Buy zero amount";
    },
    {
      code: 6021;
      name: "notEnoughTokensToBuy";
      msg: "Not enough tokens to buy";
    },
    {
      code: 6022;
      name: "sellZeroAmount";
      msg: "Sell zero amount";
    },
    {
      code: 6023;
      name: "notEnoughTokensToSell";
      msg: "Not enough tokens to sell";
    },
    {
      code: 6024;
      name: "overflow";
      msg: "overflow";
    },
    {
      code: 6025;
      name: "truncation";
      msg: "truncation";
    },
    {
      code: 6026;
      name: "divisionByZero";
      msg: "Division by zero";
    },
    {
      code: 6027;
      name: "notEnoughRemainingAccounts";
      msg: "Not enough remaining accounts";
    },
    {
      code: 6028;
      name: "allFeeRecipientsShouldBeNonZero";
      msg: "All fee recipients should be non-zero";
    },
    {
      code: 6029;
      name: "unsortedNotUniqueFeeRecipients";
      msg: "Unsorted or not unique fee recipients";
    },
    {
      code: 6030;
      name: "creatorShouldNotBeZero";
      msg: "Creator should not be zero";
    },
    {
      code: 6031;
      name: "startTimeInThePast";
    },
    {
      code: 6032;
      name: "endTimeInThePast";
    },
    {
      code: 6033;
      name: "endTimeBeforeStartTime";
    },
    {
      code: 6034;
      name: "timeRangeTooLarge";
    },
    {
      code: 6035;
      name: "endTimeBeforeCurrentDay";
    },
    {
      code: 6036;
      name: "supplyUpdateForFinishedRange";
    },
    {
      code: 6037;
      name: "dayIndexAfterEndIndex";
    },
    {
      code: 6038;
      name: "dayInActiveRange";
    },
    {
      code: 6039;
      name: "invalidIncentiveMint";
    },
    {
      code: 6040;
      name: "buyNotEnoughSolToCoverRent";
      msg: "Buy: Not enough SOL to cover for rent exemption.";
    },
    {
      code: 6041;
      name: "buyNotEnoughSolToCoverFees";
      msg: "Buy: Not enough SOL to cover for fees.";
    },
    {
      code: 6042;
      name: "buySlippageBelowMinTokensOut";
      msg: "Slippage: Would buy less tokens than expected min_tokens_out";
    },
    {
      code: 6043;
      name: "nameTooLong";
    },
    {
      code: 6044;
      name: "symbolTooLong";
    },
    {
      code: 6045;
      name: "uriTooLong";
    },
    {
      code: 6046;
      name: "createV2Disabled";
    },
    {
      code: 6047;
      name: "cpitializeMayhemFailed";
    },
    {
      code: 6048;
      name: "mayhemModeDisabled";
    },
    {
      code: 6049;
      name: "creatorMigratedToSharingConfig";
      msg: "creator has been migrated to sharing config, use pump_fees::reset_fee_sharing_config instead";
    },
    {
      code: 6050;
      name: "unableToDistributeCreatorVaultMigratedToSharingConfig";
      msg: "creator_vault has been migrated to sharing config, use pump:distribute_creator_fees instead";
    },
    {
      code: 6051;
      name: "sharingConfigNotActive";
      msg: "Sharing config is not active";
    },
    {
      code: 6052;
      name: "unableToDistributeCreatorFeesToExecutableRecipient";
      msg: "The recipient account is executable, so it cannot receive lamports, remove it from the team first";
    },
    {
      code: 6053;
      name: "BondingCurveAndSharingConfigCreatorMismatch";
      msg: "Bonding curve creator does not match sharing config";
    },
    {
      code: 6054;
      name: "shareholdersAndRemainingAccountsMismatch";
      msg: "Remaining accounts do not match shareholders, make sure to pass exactly the same pubkeys in the same order";
    },
    {
      code: 6055;
      name: "invalidShareBps";
      msg: "Share bps must be greater than 0";
    },
    {
      code: 6056;
      name: "cashbackNotEnabled";
      msg: "Cashback is not enabled";
    },
  ];
  types: [
    {
      name: "adminSetCreatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "adminSetCreatorAuthority";
            type: "pubkey";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "bondingCurve";
            type: "pubkey";
          },
          {
            name: "oldCreator";
            type: "pubkey";
          },
          {
            name: "newCreator";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "adminSetIdlAuthorityEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "idlAuthority";
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
      name: "collectCreatorFeeEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "creator";
            type: "pubkey";
          },
          {
            name: "creatorFee";
            type: "u64";
          },
        ];
      };
    },
    {
      name: "completeEvent";
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
            name: "bondingCurve";
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
      name: "completePumpAmmMigrationEvent";
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
            name: "mintAmount";
            type: "u64";
          },
          {
            name: "solAmount";
            type: "u64";
          },
          {
            name: "poolMigrationFee";
            type: "u64";
          },
          {
            name: "bondingCurve";
            type: "pubkey";
          },
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "pool";
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
      name: "createEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "name";
            type: "string";
          },
          {
            name: "symbol";
            type: "string";
          },
          {
            name: "uri";
            type: "string";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "bondingCurve";
            type: "pubkey";
          },
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "creator";
            type: "pubkey";
          },
          {
            name: "timestamp";
            type: "i64";
          },
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
            name: "tokenTotalSupply";
            type: "u64";
          },
          {
            name: "tokenProgram";
            type: "pubkey";
          },
          {
            name: "isMayhemMode";
            type: "bool";
          },
          {
            name: "isCashbackEnabled";
            type: "bool";
          },
        ];
      };
    },
    {
      name: "distributeCreatorFeesEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "bondingCurve";
            type: "pubkey";
          },
          {
            name: "sharingConfig";
            type: "pubkey";
          },
          {
            name: "admin";
            type: "pubkey";
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
          {
            name: "distributed";
            type: "u64";
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
          {
            name: "timestamp";
            type: "i64";
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
      name: "global";
      type: {
        kind: "struct";
        fields: [
          {
            name: "initialized";
            docs: ["Unused"];
            type: "bool";
          },
          {
            name: "authority";
            type: "pubkey";
          },
          {
            name: "feeRecipient";
            type: "pubkey";
          },
          {
            name: "initialVirtualTokenReserves";
            type: "u64";
          },
          {
            name: "initialVirtualSolReserves";
            type: "u64";
          },
          {
            name: "initialRealTokenReserves";
            type: "u64";
          },
          {
            name: "tokenTotalSupply";
            type: "u64";
          },
          {
            name: "feeBasisPoints";
            type: "u64";
          },
          {
            name: "withdrawAuthority";
            type: "pubkey";
          },
          {
            name: "enableMigrate";
            docs: ["Unused"];
            type: "bool";
          },
          {
            name: "poolMigrationFee";
            type: "u64";
          },
          {
            name: "creatorFeeBasisPoints";
            type: "u64";
          },
          {
            name: "feeRecipients";
            type: {
              array: ["pubkey", 7];
            };
          },
          {
            name: "setCreatorAuthority";
            type: "pubkey";
          },
          {
            name: "adminSetCreatorAuthority";
            type: "pubkey";
          },
          {
            name: "createV2Enabled";
            type: "bool";
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
      name: "migrateBondingCurveCreatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "bondingCurve";
            type: "pubkey";
          },
          {
            name: "sharingConfig";
            type: "pubkey";
          },
          {
            name: "oldCreator";
            type: "pubkey";
          },
          {
            name: "newCreator";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "minimumDistributableFeeEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "minimumRequired";
            type: "u64";
          },
          {
            name: "distributableFees";
            type: "u64";
          },
          {
            name: "canDistribute";
            type: "bool";
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
      name: "setCreatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "bondingCurve";
            type: "pubkey";
          },
          {
            name: "creator";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "setMetaplexCreatorEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "bondingCurve";
            type: "pubkey";
          },
          {
            name: "metadata";
            type: "pubkey";
          },
          {
            name: "creator";
            type: "pubkey";
          },
        ];
      };
    },
    {
      name: "setParamsEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "initialVirtualTokenReserves";
            type: "u64";
          },
          {
            name: "initialVirtualSolReserves";
            type: "u64";
          },
          {
            name: "initialRealTokenReserves";
            type: "u64";
          },
          {
            name: "finalRealSolReserves";
            type: "u64";
          },
          {
            name: "tokenTotalSupply";
            type: "u64";
          },
          {
            name: "feeBasisPoints";
            type: "u64";
          },
          {
            name: "withdrawAuthority";
            type: "pubkey";
          },
          {
            name: "enableMigrate";
            type: "bool";
          },
          {
            name: "poolMigrationFee";
            type: "u64";
          },
          {
            name: "creatorFeeBasisPoints";
            type: "u64";
          },
          {
            name: "feeRecipients";
            type: {
              array: ["pubkey", 8];
            };
          },
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "setCreatorAuthority";
            type: "pubkey";
          },
          {
            name: "adminSetCreatorAuthority";
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
      name: "tradeEvent";
      docs: ['ix_name: "buy" | "sell" | "buy_exact_sol_in"'];
      type: {
        kind: "struct";
        fields: [
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "solAmount";
            type: "u64";
          },
          {
            name: "tokenAmount";
            type: "u64";
          },
          {
            name: "isBuy";
            type: "bool";
          },
          {
            name: "user";
            type: "pubkey";
          },
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "virtualSolReserves";
            type: "u64";
          },
          {
            name: "virtualTokenReserves";
            type: "u64";
          },
          {
            name: "realSolReserves";
            type: "u64";
          },
          {
            name: "realTokenReserves";
            type: "u64";
          },
          {
            name: "feeRecipient";
            type: "pubkey";
          },
          {
            name: "feeBasisPoints";
            type: "u64";
          },
          {
            name: "fee";
            type: "u64";
          },
          {
            name: "creator";
            type: "pubkey";
          },
          {
            name: "creatorFeeBasisPoints";
            type: "u64";
          },
          {
            name: "creatorFee";
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
            name: "ixName";
            type: "string";
          },
          {
            name: "mayhemMode";
            type: "bool";
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
      name: "updateGlobalAuthorityEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "global";
            type: "pubkey";
          },
          {
            name: "authority";
            type: "pubkey";
          },
          {
            name: "newAuthority";
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
      name: "updateMayhemVirtualParamsEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "timestamp";
            type: "i64";
          },
          {
            name: "mint";
            type: "pubkey";
          },
          {
            name: "virtualTokenReserves";
            type: "u64";
          },
          {
            name: "virtualSolReserves";
            type: "u64";
          },
          {
            name: "newVirtualTokenReserves";
            type: "u64";
          },
          {
            name: "newVirtualSolReserves";
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
  ];
}
