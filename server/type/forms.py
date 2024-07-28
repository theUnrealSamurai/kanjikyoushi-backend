from django import forms

class TypedTextForm(forms.Form):
    typed_text = forms.CharField(label='Type the sentence', max_length=1000, widget=forms.TextInput(attrs={'autofocus': 'autofocus'}) ) 

