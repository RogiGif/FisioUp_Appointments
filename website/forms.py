from django import forms


class ContactForm(forms.Form):
    name = forms.CharField(max_length=120, label="Nome")
    email = forms.EmailField(label="Email")
    phone = forms.CharField(max_length=30, required=False, label="Telefone")
    subject = forms.CharField(max_length=180, required=False, label="Assunto")
    message = forms.CharField(widget=forms.Textarea, label="Mensagem")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if name == "message":
                field.widget.attrs.setdefault("rows", 5)
                field.widget.attrs.setdefault("placeholder", "Descreva como podemos ajudar.")
            elif name == "subject":
                field.widget.attrs.setdefault("placeholder", "Ex.: Pedido de informação")
            elif name == "phone":
                field.widget.attrs.setdefault("placeholder", "Ex.: +351 912 345 678")
            elif name == "name":
                field.widget.attrs.setdefault("placeholder", "O seu nome")
            elif name == "email":
                field.widget.attrs.setdefault("placeholder", "nome@exemplo.com")
