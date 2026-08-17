package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

const rcpProtocolVersion = 2

type supervisorIdentity struct {
	ProtocolVersion int
	RunID           string
	PoolID          string
	PoolInstanceID  string
	Generation      int
	Token           string
}

func processEnvironment() map[string]string {
	env := make(map[string]string, len(os.Environ()))
	for _, item := range os.Environ() {
		key, value, ok := strings.Cut(item, "=")
		if ok {
			env[key] = value
		}
	}
	return env
}

func supervisorIdentityFromEnv(env map[string]string) (supervisorIdentity, error) {
	var out supervisorIdentity
	out.ProtocolVersion = rcpProtocolVersion

	var err error
	if out.RunID, err = boundedIdentityText(env["DSWARM_RUN_ID"], "run_id", 256); err != nil {
		return supervisorIdentity{}, err
	}
	if out.PoolID, err = boundedIdentityText(env["DSWARM_POOL_ID"], "pool_id", 256); err != nil {
		return supervisorIdentity{}, err
	}
	if out.PoolInstanceID, err = canonicalUUID4(env["DSWARM_POOL_INSTANCE_ID"]); err != nil {
		return supervisorIdentity{}, err
	}

	rawGeneration := env["DSWARM_POOL_GENERATION"]
	generation, err := strconv.Atoi(rawGeneration)
	if err != nil || generation <= 0 || generation > 2_147_483_647 || strconv.Itoa(generation) != rawGeneration {
		return supervisorIdentity{}, fmt.Errorf("generation must be a canonical positive base-10 integer")
	}
	out.Generation = generation

	inlineToken := env["DSWARM_CONTROL_TOKEN"]
	tokenFile := env["DSWARM_CONTROL_TOKEN_FILE"]
	if inlineToken != "" && tokenFile != "" {
		return supervisorIdentity{}, fmt.Errorf("control token source is ambiguous")
	}
	if tokenFile != "" {
		raw, readErr := os.ReadFile(tokenFile)
		if readErr != nil {
			return supervisorIdentity{}, fmt.Errorf("read control token file: %w", readErr)
		}
		inlineToken = strings.TrimSpace(string(raw))
	}
	if inlineToken == "" || len(inlineToken) > 4096 || containsControl(inlineToken) {
		return supervisorIdentity{}, fmt.Errorf("control token must be a bounded non-empty value")
	}
	out.Token = inlineToken
	return out, nil
}

func boundedIdentityText(value, field string, maxLength int) (string, error) {
	if value == "" || len(value) > maxLength {
		return "", fmt.Errorf("%s must be a bounded non-empty string", field)
	}
	if strings.TrimSpace(value) != value || containsControl(value) {
		return "", fmt.Errorf("%s contains invalid characters", field)
	}
	return value, nil
}

func containsControl(value string) bool {
	for _, r := range value {
		if r < 32 || r == 127 {
			return true
		}
	}
	return false
}

func canonicalUUID4(value string) (string, error) {
	if len(value) != 36 || value[8] != '-' || value[13] != '-' || value[18] != '-' || value[23] != '-' {
		return "", fmt.Errorf("pool_instance_id must be a canonical UUID4")
	}
	for i := 0; i < len(value); i++ {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			continue
		}
		c := value[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return "", fmt.Errorf("pool_instance_id must be a canonical UUID4")
		}
	}
	if value[14] != '4' || !strings.ContainsRune("89ab", rune(value[19])) {
		return "", fmt.Errorf("pool_instance_id must be a canonical UUID4")
	}
	return value, nil
}
