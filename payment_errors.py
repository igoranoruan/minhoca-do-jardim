# Mapeamento de recusas comuns do Mercado Pago para mensagens amigáveis.
PAYMENT_ERRORS = {
    "cc_rejected_bad_filled_card_number": "Número do cartão incorreto.",
    "cc_rejected_bad_filled_date": "Data de validade incorreta.",
    "cc_rejected_bad_filled_security_code": "Código de segurança (CVV) inválido.",
    "cc_rejected_bad_filled_other": "Verifique os dados do cartão inseridos.",
    "cc_rejected_insufficient_amount": "Saldo insuficiente no cartão.",
    "cc_rejected_call_for_authorize": "Pagamento não autorizado pela administradora.",
    "cc_rejected_card_disabled": "Cartão bloqueado ou desativado.",
    "cc_rejected_duplicated_payment": "Pagamento duplicado detectado.",
    "cc_rejected_high_risk": "Pagamento recusado pela análise de segurança do Mercado Pago.",
}


def get_friendly_error(error_code: str) -> str:
    return PAYMENT_ERRORS.get(
        error_code,
        "Ocorreu um erro ao processar o pagamento. Tente novamente.",
    )
